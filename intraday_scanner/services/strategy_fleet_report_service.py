"""Horizon-separated reporting for the Dawnstrike paper strategy fleet.

AlphaOps intraday reconciliation and v2 daily-swing PaperOps have different
capital and return contracts.  This module presents them together without
aggregating them: every row and summary retains its source, horizon, cohort,
mode, and return scale.  Missing returns remain missing.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import (
    FIRST_ELIGIBLE_ACTIVATION_POLICY,
    market_session,
    registration_coverage_inception_date,
)
from intraday_scanner.paper_ops_root import production_paper_ops_root
from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths, _recover_pending_transaction
from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
from intraday_scanner.v2.strategies import build_strategy_catalog

ALPHAOPS_HORIZON = "intraday"
PAPEROPS_HORIZON = "daily_swing"
ALPHAOPS_SOURCE = "alphaops_sqlite"
PAPEROPS_SOURCE = "v2_paper_ops"
PAPEROPS_FORWARD_COHORT = "paper_forward"
DEFAULT_BENCHMARK_SYMBOL = "SPY"
PAPEROPS_BENCHMARK_ID = "benchmark_buy_hold_equal_weight"
CASH_BASELINE_ID = "cash_no_trade_baseline"
CASH_BASELINE_SOURCE = "strategy_catalog:cash_no_trade_baseline:v1.0"
_OFFICIAL_DELIVERY_STATUSES = {"delivered", "delivered_legacy"}
_LEGACY_ACTIVATION_POLICY = FIRST_ELIGIBLE_ACTIVATION_POLICY

PaperSeriesIdentity = tuple[str, str, str, str]

_DAILY_FIELDS = (
    "date",
    "source_system",
    "horizon",
    "mode",
    "strategy_id",
    "strategy_version",
    "execution_policy_version",
    "strategy_semantics_fingerprint",
    "strategy_status",
    "cohort",
    "return_observed",
    "source_return_scale",
    "daily_return_source_value",
    "cumulative_return_source_value",
    "normalized_daily_return_pct",
    "normalized_cumulative_return_pct",
    "net_pnl",
    "realized_net_pnl",
    "allocated_notional",
    "trades_opened",
    "trades_closed",
    "wins",
    "losses",
    "flats",
    "unresolved_count",
    "evidence_status",
    "return_semantics",
    "benchmark_id",
    "benchmark_return_pct",
    "benchmark_cumulative_return_pct",
    "benchmark_source",
    "benchmark_source_quality",
    "benchmark_comparison_status",
    "excess_return_vs_benchmark_pct",
    "cash_baseline_id",
    "cash_return_pct",
    "cash_cumulative_return_pct",
    "cash_source",
    "excess_return_vs_cash_pct",
    "source_run_id",
)

_SUMMARY_FIELDS = (
    "source_system",
    "horizon",
    "mode",
    "strategy_id",
    "strategy_version",
    "execution_policy_version",
    "strategy_status",
    "cohort",
    "first_date",
    "last_date",
    "daily_row_count",
    "return_observation_count",
    "missing_return_count",
    "trades_opened",
    "trades_closed",
    "wins",
    "losses",
    "flats",
    "unresolved_count",
    "total_allocated_notional",
    "total_realized_net_pnl",
    "allocation_evidence_missing_count",
    "weighted_realized_return_pct",
    "hypothetical_compounded_daily_return_pct",
    "latest_cumulative_return_source_value",
    "source_return_scale",
    "normalized_cumulative_return_pct",
    "cumulative_return_semantics",
    "benchmark_id",
    "benchmark_observation_count",
    "benchmark_missing_count",
    "normalized_benchmark_cumulative_return_pct",
    "normalized_excess_return_vs_benchmark_pct",
    "benchmark_cumulative_semantics",
    "cash_baseline_id",
    "normalized_cash_cumulative_return_pct",
    "normalized_excess_return_vs_cash_pct",
)


def build_strategy_fleet_report(
    *,
    db_path: str | Path = "data/shadow_real.sqlite",
    paper_ops_root: str | Path | None = None,
    out_dir: str | Path = "outputs/strategy_fleet",
    start: str | None = None,
    end: str | None = None,
    benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL,
) -> dict[str, Any]:
    """Build deterministic daily and cumulative paper-strategy artifacts.

    ``complete`` means both required sources yielded valid in-range evidence
    for the expected strategy fleet. ``partial`` means only part of that
    evidence was usable. ``failed`` means neither source yielded a reportable
    row.  Artifacts are still written for partial and failed runs so absence is
    inspectable rather than silent.
    """

    db = Path(db_path)
    paper_root = production_paper_ops_root(override=paper_ops_root)
    output = Path(out_dir)
    csv_path = paper_root / "calendar" / "strategy_daily_returns.csv"
    warnings: list[str] = []
    source_state: dict[str, dict[str, Any]] = {}
    calendar_truth_evidence: dict[str, object] = {}
    source_bar_truth_evidence: dict[str, object] = {}
    paper_truth_ready = False
    try:
        _recover_pending_transaction(PaperOpsPaths.create(paper_root))
        calendar_truth = verify_calendar_truth(output_root=paper_root)
        source_bar_truth = verify_source_bar_truth(
            output_root=paper_root,
            mode="forward",
        )
        calendar_truth_evidence = calendar_truth.to_dict()
        source_bar_truth_evidence = source_bar_truth.to_dict()
        paper_truth_ready = (
            calendar_truth.status == "passed" and source_bar_truth.status == "passed"
        )
        if not paper_truth_ready:
            for field in (
                "duplicate_rows",
                "missing_rows",
                "math_mismatches",
                "ledger_mismatches",
            ):
                raw_values = calendar_truth_evidence.get(field)
                values = [
                    str(value)
                    for value in (
                        raw_values if isinstance(raw_values, list | tuple) else []
                    )
                    if str(value)
                ]
                if values:
                    warnings.append(
                        f"PaperOps calendar truth {field}: " + " | ".join(values[:5])
                    )
            warnings.extend(
                f"PaperOps source-bar truth: {warning}"
                for warning in source_bar_truth.warnings[:10]
            )
    except Exception as exc:  # Standalone reports fail closed on recovery/truth errors.
        warnings.append(
            "PaperOps transaction recovery/calendar/source-bar truth verification failed: "
            f"{str(exc)[:500]}"
        )

    alpha_rows = _read_alphaops_rows(
        db,
        start=start,
        end=end,
        warnings=warnings,
        state=source_state,
        benchmark_symbol=benchmark_symbol,
    )
    if paper_truth_ready:
        paper_rows = _read_paperops_rows(
            csv_path,
            start=start,
            end=end,
            warnings=warnings,
            state=source_state,
        )
        source_state[PAPEROPS_SOURCE]["calendar_truth"] = calendar_truth_evidence
        source_state[PAPEROPS_SOURCE]["source_bar_truth"] = source_bar_truth_evidence
    else:
        paper_rows = []
        expected_strategy_ids = sorted(
            strategy.strategy_id
            for strategy in build_strategy_catalog()
            if strategy.status
            not in {"baseline", "benchmark", "quarantined", "rejected", "parked"}
        )
        source_state[PAPEROPS_SOURCE] = {
            "path": str(csv_path),
            "required_mode": "forward",
            "expected_strategy_ids": expected_strategy_ids,
            "present_strategy_ids": [],
            "status": "invalid",
            "row_count": 0,
            "calendar_truth": calendar_truth_evidence,
            "source_bar_truth": source_bar_truth_evidence,
        }
    daily_rows = sorted(
        [*alpha_rows, *paper_rows],
        key=lambda row: (
            str(row["horizon"]),
            str(row["strategy_id"]),
            str(row["strategy_version"]),
            str(row["execution_policy_version"]),
            str(row["cohort"]),
            str(row["date"]),
        ),
    )
    summaries = _build_summaries(daily_rows)
    usable_sources = sum(bool(rows) for rows in (alpha_rows, paper_rows))
    has_source_gap = any(state.get("status") != "complete" for state in source_state.values())
    if usable_sources == 0:
        status = "failed"
    elif usable_sources < 2 or has_source_gap or warnings:
        status = "partial"
    else:
        status = "complete"

    payload: dict[str, Any] = {
        "schema_version": "dawnstrike.strategy_fleet_report.v3",
        "status": status,
        "date_range": {"start": _date_part(start), "end": _date_part(end)},
        "return_contract": {
            "aggregation_across_horizons": "prohibited",
            "alphaops": (
                "Percent return on allocated paper-trade capital. No-entry days are N/A, "
                "not zero. Cumulative performance is canonical realized net P&L divided "
                "by canonical allocated notional; daily compounding is separately labeled "
                "hypothetical."
            ),
            "v2_paper_ops": (
                "Fractional strategy-account return from the source calendar; normalized "
                "percentage fields multiply the source value by 100."
            ),
            "benchmark": (
                "Comparisons stay within source, horizon, mode, and date. Missing or "
                "incompatible benchmark evidence remains N/A."
            ),
            "cash": (
                "The explicit catalog cash_no_trade_baseline is a zero-return policy "
                "baseline, not an interest-bearing cash-rate estimate."
            ),
        },
        "sources": source_state,
        "paperops_calendar_truth": calendar_truth_evidence,
        "warnings": sorted(set(warnings)),
        "daily_rows": daily_rows,
        "strategy_summaries": summaries,
    }
    artifacts = _write_artifacts(output, payload)
    return {**payload, "artifacts": artifacts}


def _read_alphaops_rows(
    db_path: Path,
    *,
    start: str | None,
    end: str | None,
    warnings: list[str],
    state: dict[str, dict[str, Any]],
    benchmark_symbol: str,
) -> list[dict[str, Any]]:
    normalized_benchmark = benchmark_symbol.upper().strip()
    source: dict[str, Any] = {
        "path": str(db_path),
        "required_table": "daily_strategy_scorecards",
        "canonical_trade_table": "strategy_paper_trades",
        "official_delivery_table": "notification_delivery_memberships",
        "benchmark_table": "benchmark_observations",
        "benchmark_symbol": normalized_benchmark,
        "status": "missing",
        "row_count": 0,
    }
    state[ALPHAOPS_SOURCE] = source
    if not db_path.is_file():
        warnings.append(f"AlphaOps SQLite database is absent: {db_path}")
        return []
    try:
        connection = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        with connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("daily_strategy_scorecards",),
            ).fetchone()
            if exists is None:
                source["status"] = "invalid"
                warnings.append("AlphaOps database has no daily_strategy_scorecards table.")
                return []
            where_sql, parameters = _market_date_where(start=start, end=end)
            raw_rows = connection.execute(
                "SELECT * FROM daily_strategy_scorecards"
                + where_sql
                + " ORDER BY market_date, strategy_id, strategy_version, cohort",
                parameters,
            ).fetchall()
            trade_table_available = _sqlite_table_exists(
                connection, "strategy_paper_trades"
            )
            trade_rows = (
                connection.execute(
                    "SELECT * FROM strategy_paper_trades"
                    + where_sql
                    + " ORDER BY market_date, strategy_id, strategy_version, cohort, trade_id",
                    parameters,
                ).fetchall()
                if trade_table_available
                else []
            )
            delivery_table_available = _sqlite_table_exists(
                connection, "notification_delivery_memberships"
            )
            delivery_rows = (
                connection.execute(
                    "SELECT signal_id, channel, delivery_status "
                    "FROM notification_delivery_memberships"
                ).fetchall()
                if delivery_table_available
                else []
            )
            benchmark_table_available = _sqlite_table_exists(
                connection, "benchmark_observations"
            )
            benchmark_where = where_sql
            benchmark_parameters: list[str] = [*parameters]
            benchmark_where += " AND symbol = ?" if benchmark_where else " WHERE symbol = ?"
            benchmark_parameters.append(normalized_benchmark)
            benchmark_rows = (
                connection.execute(
                    "SELECT * FROM benchmark_observations"
                    + benchmark_where
                    + " ORDER BY market_date, observed_at, benchmark_id",
                    benchmark_parameters,
                ).fetchall()
                if benchmark_table_available
                else []
            )
    except sqlite3.Error as exc:
        source["status"] = "invalid"
        warnings.append(f"AlphaOps scorecards could not be read: {exc}")
        return []
    finally:
        if "connection" in locals():
            connection.close()

    official_signal_ids = {
        str(row["signal_id"])
        for row in delivery_rows
        if str(row["channel"] or "").strip().lower() == "telegram"
        and str(row["delivery_status"] or "").strip().lower()
        in _OFFICIAL_DELIVERY_STATUSES
    }
    trade_evidence = _alpha_trade_evidence(
        trade_rows,
        official_signal_ids=official_signal_ids,
    )
    benchmark_by_date, benchmark_integrity_issues = _alpha_benchmark_evidence(
        benchmark_rows,
        symbol=normalized_benchmark,
    )
    rows: list[dict[str, Any]] = []
    incomplete_scorecards = 0
    allocation_evidence_missing = 0
    for raw in raw_rows:
        value = _optional_float(raw["return_on_allocated_capital_pct"])
        evidence_status = str(raw["reconciliation_status"])
        if evidence_status != "complete":
            incomplete_scorecards += 1
        market_date = str(raw["market_date"])[:10]
        strategy_id = str(raw["strategy_id"])
        strategy_version = str(raw["strategy_version"])
        execution_policy_version = str(raw["execution_policy_version"])
        cohort = str(raw["cohort"])
        all_key = (
            market_date,
            strategy_id,
            strategy_version,
            execution_policy_version,
        )
        cohort_key = (*all_key, cohort)
        canonical = (
            trade_evidence["all"].get(all_key)
            if cohort == "algorithm_selected"
            else trade_evidence["official"].get(all_key)
            if cohort == "official_telegram"
            else trade_evidence["cohort"].get(cohort_key)
        )
        closed_count = _optional_int(raw["closed_count"])
        if canonical is None and trade_table_available and not closed_count:
            canonical = {"allocated_notional": 0.0, "realized_net_pnl": 0.0, "count": 0}
        if value is not None and (
            canonical is None or float(canonical["allocated_notional"]) <= 0
        ):
            allocation_evidence_missing += 1
            warnings.append(
                "AlphaOps observed return lacks canonical allocated-notional evidence: "
                f"{market_date}/{strategy_id}/{cohort}"
            )
        if canonical is not None and closed_count != int(canonical["count"]):
            warnings.append(
                "AlphaOps scorecard/trade count mismatch: "
                f"{market_date}/{strategy_id}/{cohort} "
                f"scorecard={closed_count} canonical={canonical['count']}"
            )
        scorecard_net_pnl = _optional_float(raw["net_pnl"])
        if (
            canonical is not None
            and scorecard_net_pnl is not None
            and abs(scorecard_net_pnl - float(canonical["realized_net_pnl"])) > 1e-6
        ):
            warnings.append(
                "AlphaOps scorecard/canonical P&L mismatch: "
                f"{market_date}/{strategy_id}/{cohort}"
            )
        benchmark = benchmark_by_date.get(market_date)
        benchmark_return = (
            _optional_float(benchmark.get("return_pct")) if benchmark else None
        )
        rows.append(
            {
                "date": market_date,
                "source_system": ALPHAOPS_SOURCE,
                "horizon": ALPHAOPS_HORIZON,
                "mode": "reconciled",
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_status": "paper_research",
                "cohort": cohort,
                "return_observed": value is not None,
                "source_return_scale": "percent_points",
                "daily_return_source_value": value,
                "cumulative_return_source_value": None,
                "normalized_daily_return_pct": value,
                "normalized_cumulative_return_pct": None,
                "net_pnl": scorecard_net_pnl,
                "realized_net_pnl": (
                    _optional_float(canonical.get("realized_net_pnl"))
                    if canonical is not None
                    else None
                ),
                "allocated_notional": (
                    _optional_float(canonical.get("allocated_notional"))
                    if canonical is not None
                    else None
                ),
                "trades_opened": _optional_int(raw["filled_count"]),
                "trades_closed": _optional_int(raw["closed_count"]),
                "wins": _optional_int(raw["wins"]),
                "losses": _optional_int(raw["losses"]),
                "flats": _optional_int(raw["flats"]),
                "unresolved_count": _optional_int(raw["unresolved_count"]),
                "evidence_status": evidence_status,
                "return_semantics": (
                    "observed_allocated_capital_return_pct"
                    if value is not None
                    else "N/A_no_closed_paper_trade_or_unresolved"
                ),
                "benchmark_id": benchmark.get("benchmark_id") if benchmark else None,
                "benchmark_return_pct": benchmark_return,
                "benchmark_cumulative_return_pct": None,
                "benchmark_source": benchmark.get("source") if benchmark else None,
                "benchmark_source_quality": (
                    benchmark.get("source_quality") if benchmark else None
                ),
                "benchmark_comparison_status": (
                    "available_same_day_open_to_close"
                    if benchmark_return is not None
                    else "missing_same_day_open_to_close_observation"
                ),
                "excess_return_vs_benchmark_pct": _subtract_optional(
                    value, benchmark_return
                ),
                "cash_baseline_id": CASH_BASELINE_ID,
                "cash_return_pct": 0.0,
                "cash_cumulative_return_pct": 0.0,
                "cash_source": CASH_BASELINE_SOURCE,
                "excess_return_vs_cash_pct": _subtract_optional(value, 0.0),
                "source_run_id": "",
            }
        )
    source["row_count"] = len(rows)
    source["canonical_trade_table_available"] = trade_table_available
    source["canonical_trade_count"] = len(trade_rows)
    source["official_delivery_table_available"] = delivery_table_available
    source["official_delivered_signal_count"] = len(official_signal_ids)
    source["allocation_evidence_missing_count"] = allocation_evidence_missing
    source["benchmark_table_available"] = benchmark_table_available
    source["benchmark_observation_count"] = len(benchmark_rows)
    source["benchmark_integrity_issue_count"] = benchmark_integrity_issues
    source["benchmark_comparison_status"] = _comparison_coverage_status(
        rows, "benchmark_return_pct"
    )
    if benchmark_integrity_issues:
        warnings.append(
            "AlphaOps benchmark evidence has "
            f"{benchmark_integrity_issues} invalid or conflicting observation(s)."
        )
    source["incomplete_scorecard_count"] = incomplete_scorecards
    source["status"] = (
        "partial" if incomplete_scorecards or allocation_evidence_missing else "complete"
    )
    if not rows:
        source["status"] = "empty"
        warnings.append("AlphaOps scorecard table yielded no rows in the requested range.")
    elif incomplete_scorecards:
        warnings.append(
            f"AlphaOps has {incomplete_scorecards} incomplete daily strategy scorecard(s)."
        )
    return rows


def _read_paperops_rows(
    csv_path: Path,
    *,
    start: str | None,
    end: str | None,
    warnings: list[str],
    state: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    catalog = {strategy.strategy_id: strategy for strategy in build_strategy_catalog()}
    expected = {
        strategy_id: strategy
        for strategy_id, strategy in catalog.items()
        if strategy.status not in {"baseline", "benchmark", "quarantined", "rejected", "parked"}
    }
    source: dict[str, Any] = {
        "path": str(csv_path),
        "required_mode": "forward",
        "expected_strategy_ids": sorted(expected),
        "present_strategy_ids": [],
        "excluded_non_forward_rows": 0,
        "excluded_unregistered_rows": 0,
        "status": "missing",
        "row_count": 0,
    }
    state[PAPEROPS_SOURCE] = source
    if not csv_path.is_file():
        warnings.append(f"PaperOps strategy calendar is absent: {csv_path}")
        return []
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            raw_rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        source["status"] = "invalid"
        warnings.append(f"PaperOps strategy calendar could not be read: {exc}")
        return []

    registry_path = csv_path.parent.parent / "state" / "strategy_registry.json"
    active_series: dict[str, tuple[str, str, str]] = {}
    registry_payload: object
    try:
        registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        source["status"] = "invalid"
        source["active_registry_status"] = "invalid"
        warnings.append(f"PaperOps active strategy registry is unavailable: {exc}")
        return []
    if not isinstance(registry_payload, list):
        source["status"] = "invalid"
        source["active_registry_status"] = "invalid"
        warnings.append("PaperOps active strategy registry is not a list.")
        return []
    for registry_row in registry_payload:
        if not isinstance(registry_row, dict):
            continue
        strategy_id = str(registry_row.get("strategy_id") or "").strip()
        strategy_version = str(registry_row.get("strategy_version") or "").strip()
        policy_version = str(
            registry_row.get("execution_policy_version") or ""
        ).strip()
        semantics = str(
            registry_row.get("strategy_semantics_fingerprint") or ""
        ).strip()
        if (
            strategy_id in expected
            and strategy_version
            and policy_version
            and semantics
            and semantics != "unknown"
        ):
            if strategy_id in active_series:
                source["status"] = "invalid"
                source["active_registry_status"] = "invalid"
                warnings.append(
                    f"PaperOps active strategy registry duplicates {strategy_id}."
                )
                return []
            active_series[strategy_id] = (
                strategy_version,
                policy_version,
                semantics,
            )
    source["active_registry_path"] = str(registry_path)
    source["active_registry_status"] = (
        "complete" if set(active_series) == set(expected) else "partial"
    )
    inception_dates, inception_issues, manifest_paths = (
        _paperops_strategy_inception_dates(
            expected_strategy_ids=set(expected),
            active_series=active_series,
            state_dir=registry_path.parent,
        )
    )
    source["strategy_semantics_manifest_path"] = str(manifest_paths[0])
    source["execution_policy_manifest_path"] = str(manifest_paths[1])
    source["strategy_registry_exact_inception_dates"] = {
        _paper_series_key(identity): inception
        for identity, inception in sorted(inception_dates.items())
    }
    source["strategy_registry_inception_dates"] = {
        identity[0]: inception
        for identity, inception in sorted(inception_dates.items())
    }
    source["strategy_registry_exact_inception_issues"] = dict(
        sorted(inception_issues.items())
    )
    source["strategy_registry_inception_issues"] = {
        key.split("|", 1)[0]: issue for key, issue in sorted(inception_issues.items())
    }
    source["strategy_registry_inception_status"] = (
        "complete" if not inception_issues else "invalid"
    )
    source["active_registry_series"] = {
        strategy_id: {
            "strategy_version": lineage[0],
            "execution_policy_version": lineage[1],
            "strategy_semantics_fingerprint": lineage[2],
            "coverage_inception_date": inception_dates.get((strategy_id, *lineage)),
        }
        for strategy_id, lineage in sorted(active_series.items())
    }
    for exact_key, issue in sorted(inception_issues.items()):
        warnings.append(
            "PaperOps strategy registry inception is ambiguous for "
            f"{exact_key}: {issue}"
        )
    if set(active_series) != set(expected):
        warnings.append(
            "PaperOps active strategy registry is missing exact champion lineage for: "
            + ", ".join(sorted(set(expected) - set(active_series)))
        )

    comparator_evidence, comparator_counts, comparator_issues = (
        _paperops_comparator_evidence(
            raw_rows,
            start=start,
            end=end,
            warnings=warnings,
        )
    )
    source["benchmark_comparator_id"] = PAPEROPS_BENCHMARK_ID
    source["benchmark_comparator_row_count"] = comparator_counts["benchmark"]
    source["cash_comparator_id"] = CASH_BASELINE_ID
    source["cash_comparator_row_count"] = comparator_counts["cash"]
    source["comparator_integrity_issue_count"] = comparator_issues

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    malformed_return = False
    for raw in sorted(
        raw_rows,
        key=lambda item: (
            str(item.get("date") or ""),
            str(item.get("strategy_id") or ""),
            str(item.get("strategy_version") or ""),
            str(item.get("execution_policy_version") or ""),
            str(item.get("strategy_semantics_fingerprint") or ""),
            str(item.get("run_id") or ""),
        ),
    ):
        mode = str(raw.get("mode") or "").strip().lower()
        if mode != "forward":
            source["excluded_non_forward_rows"] += 1
            continue
        row_date = str(raw.get("date") or "")[:10]
        if not _in_range(row_date, start=start, end=end):
            continue
        strategy_id = str(raw.get("strategy_id") or "").strip()
        if strategy_id in {PAPEROPS_BENCHMARK_ID, CASH_BASELINE_ID}:
            continue
        if strategy_id not in expected:
            source["excluded_unregistered_rows"] += 1
            warnings.append(
                f"Ignored unregistered forward PaperOps strategy: {strategy_id or '<blank>'}"
            )
            continue
        strategy_version = str(raw.get("strategy_version") or "").strip()
        execution_policy_version = str(
            raw.get("execution_policy_version") or ""
        ).strip()
        strategy_semantics_fingerprint = str(
            raw.get("strategy_semantics_fingerprint") or ""
        ).strip()
        if active_series.get(strategy_id) != (
            strategy_version,
            execution_policy_version,
            strategy_semantics_fingerprint,
        ):
            source["excluded_unregistered_rows"] += 1
            warnings.append(
                "Ignored non-champion PaperOps series: "
                f"{strategy_id}/{strategy_version or '<blank>'}/"
                f"{execution_policy_version or '<blank>'}/"
                f"{strategy_semantics_fingerprint or '<blank>'}"
            )
            continue
        row_key = (
            row_date,
            strategy_id,
            strategy_version,
            execution_policy_version,
            strategy_semantics_fingerprint,
        )
        if row_key in seen:
            warnings.append(
                "Duplicate forward PaperOps calendar series row: "
                f"{row_date}/{strategy_id}/{strategy_version}/{execution_policy_version}"
            )
            continue
        seen.add(row_key)
        if (
            not strategy_version
            or not execution_policy_version
            or not strategy_semantics_fingerprint
        ):
            malformed_return = True
            warnings.append(
                "PaperOps row is missing exact strategy/policy lineage: "
                f"{row_date}/{strategy_id}"
            )
        daily = _optional_float(raw.get("daily_return_pct"))
        cumulative = _optional_float(raw.get("cumulative_return_pct"))
        if daily is None:
            malformed_return = True
            warnings.append(f"PaperOps daily return is missing: {row_date}/{strategy_id}")
        if cumulative is None:
            malformed_return = True
            warnings.append(f"PaperOps cumulative return is missing: {row_date}/{strategy_id}")
        benchmark = comparator_evidence["benchmark"].get(row_date)
        benchmark_daily = (
            _optional_float(benchmark.get("daily_return_pct")) if benchmark else None
        )
        benchmark_cumulative = (
            _optional_float(benchmark.get("cumulative_return_pct")) if benchmark else None
        )
        cash = comparator_evidence["cash"].get(row_date) or {
            "daily_return_pct": 0.0,
            "cumulative_return_pct": 0.0,
            "source": CASH_BASELINE_SOURCE,
        }
        cash_daily = _optional_float(cash.get("daily_return_pct"))
        cash_cumulative = _optional_float(cash.get("cumulative_return_pct"))
        normalized_daily = _fraction_to_pct(daily)
        normalized_cumulative = _fraction_to_pct(cumulative)
        rows.append(
            {
                "date": row_date,
                "source_system": PAPEROPS_SOURCE,
                "horizon": PAPEROPS_HORIZON,
                "mode": "forward",
                "strategy_id": strategy_id,
                "strategy_version": strategy_version or "legacy_unspecified",
                "execution_policy_version": (
                    execution_policy_version or "legacy_unspecified"
                ),
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "strategy_status": str(raw.get("strategy_status") or expected[strategy_id].status),
                "cohort": PAPEROPS_FORWARD_COHORT,
                "return_observed": daily is not None,
                "source_return_scale": "fraction_of_strategy_equity",
                "daily_return_source_value": daily,
                "cumulative_return_source_value": cumulative,
                "normalized_daily_return_pct": normalized_daily,
                "normalized_cumulative_return_pct": normalized_cumulative,
                "net_pnl": _optional_float(raw.get("total_pnl")),
                "realized_net_pnl": None,
                "allocated_notional": None,
                "trades_opened": _optional_int(raw.get("trades_opened")),
                "trades_closed": _optional_int(raw.get("trades_closed")),
                "wins": _optional_int(raw.get("wins")),
                "losses": _optional_int(raw.get("losses")),
                "flats": _optional_int(raw.get("flats")),
                "unresolved_count": None,
                "evidence_status": "forward_paper",
                "return_semantics": (
                    "strategy_account_fractional_return"
                    if daily is not None
                    else "N/A_missing_source_return"
                ),
                "benchmark_id": PAPEROPS_BENCHMARK_ID if benchmark else None,
                "benchmark_return_pct": benchmark_daily,
                "benchmark_cumulative_return_pct": benchmark_cumulative,
                "benchmark_source": benchmark.get("source") if benchmark else None,
                "benchmark_source_quality": (
                    "forward_paper_comparator" if benchmark else None
                ),
                "benchmark_comparison_status": (
                    "available_same_horizon_mode"
                    if benchmark_daily is not None and benchmark_cumulative is not None
                    else "missing_same_horizon_mode_comparator"
                ),
                "excess_return_vs_benchmark_pct": _subtract_optional(
                    normalized_daily, benchmark_daily
                ),
                "cash_baseline_id": CASH_BASELINE_ID,
                "cash_return_pct": cash_daily,
                "cash_cumulative_return_pct": cash_cumulative,
                "cash_source": str(cash.get("source") or CASH_BASELINE_SOURCE),
                "excess_return_vs_cash_pct": _subtract_optional(
                    normalized_daily, cash_daily
                ),
                "source_run_id": str(raw.get("run_id") or ""),
            }
        )

    eligible_rows: list[dict[str, Any]] = []
    pre_inception_rows: list[str] = []
    for row in rows:
        identity = _paper_row_identity(row)
        row_date = str(row["date"])
        inception_date = inception_dates.get(identity)
        if inception_date is not None and row_date < inception_date:
            label = f"{row_date}|{_paper_series_key(identity)}"
            pre_inception_rows.append(label)
            warnings.append(
                "Excluded impossible pre-inception forward PaperOps row: " + label
            )
            continue
        eligible_rows.append(row)
    rows = eligible_rows
    source["excluded_pre_inception_rows"] = len(pre_inception_rows)
    source["pre_inception_exact_rows"] = sorted(pre_inception_rows)

    present = sorted({str(row["strategy_id"]) for row in rows})
    coverage_dates = _paperops_coverage_dates(
        rows,
        start=start,
        end=end,
        warnings=warnings,
    )
    expected_by_date: dict[str, list[str]] = {}
    expected_exact_by_date: dict[str, list[str]] = {}
    not_yet_registered_by_date: dict[str, list[str]] = {}
    not_yet_registered_exact_by_date: dict[str, list[str]] = {}
    missing_by_date: dict[str, list[str]] = {}
    missing_exact_by_date: dict[str, list[str]] = {}
    coverage_status_by_date: dict[str, dict[str, str]] = {}
    required_any: set[str] = set()
    active_identities = {
        (strategy_id, *lineage) for strategy_id, lineage in active_series.items()
    }
    for row_date in coverage_dates:
        date_series = {
            _paper_row_identity(row) for row in rows if str(row["date"]) == row_date
        }
        date_strategy_ids = {identity[0] for identity in date_series}
        not_yet_registered_series = sorted(
            identity
            for identity, inception_date in inception_dates.items()
            if row_date < inception_date
        )
        required_series = sorted(active_identities - set(not_yet_registered_series))
        required = sorted(identity[0] for identity in required_series)
        not_yet_registered = sorted(
            identity[0] for identity in not_yet_registered_series
        )
        required_any.update(required)
        date_missing_series = sorted(set(required_series) - date_series)
        date_missing = sorted(identity[0] for identity in date_missing_series)
        expected_by_date[row_date] = required
        expected_exact_by_date[row_date] = [
            _paper_series_key(identity) for identity in required_series
        ]
        coverage_status_by_date[row_date] = {
            strategy_id: (
                "not yet registered"
                if strategy_id in not_yet_registered
                else "present"
                if strategy_id in date_strategy_ids
                else "missing"
            )
            for strategy_id in sorted(expected)
        }
        if not_yet_registered:
            not_yet_registered_by_date[row_date] = not_yet_registered
            not_yet_registered_exact_by_date[row_date] = [
                _paper_series_key(identity) for identity in not_yet_registered_series
            ]
        if date_missing:
            missing_by_date[row_date] = date_missing
            missing_exact_by_date[row_date] = [
                _paper_series_key(identity) for identity in date_missing_series
            ]
    missing = sorted(required_any - set(present))
    source["present_strategy_ids"] = present
    source["missing_strategy_ids"] = missing
    source["missing_strategy_ids_by_date"] = missing_by_date
    source["missing_exact_strategy_series_by_date"] = missing_exact_by_date
    source["expected_strategy_ids_by_date"] = expected_by_date
    source["expected_exact_strategy_series_by_date"] = expected_exact_by_date
    source["not_yet_registered_strategy_ids_by_date"] = not_yet_registered_by_date
    source["not_yet_registered_exact_strategy_series_by_date"] = (
        not_yet_registered_exact_by_date
    )
    source["strategy_coverage_status_by_date"] = coverage_status_by_date
    source["benchmark_comparison_status"] = _comparison_coverage_status(
        rows, "benchmark_return_pct"
    )
    source["cash_comparison_status"] = _comparison_coverage_status(
        rows, "cash_return_pct"
    )
    source["row_count"] = len(rows)
    if not rows:
        source["status"] = "empty"
        warnings.append(
            "PaperOps calendar yielded no registered forward rows in the requested range."
        )
    elif (
        missing
        or missing_by_date
        or malformed_return
        or pre_inception_rows
        or source["active_registry_status"] != "complete"
        or source["strategy_registry_inception_status"] != "complete"
    ):
        source["status"] = "partial"
        if missing:
            warnings.append(
                "PaperOps forward calendar is missing registered strategies: "
                + ", ".join(missing)
            )
        for row_date, date_missing in missing_by_date.items():
            warnings.append(
                f"PaperOps forward calendar is incomplete on {row_date}: "
                + ", ".join(date_missing)
            )
    else:
        source["status"] = "complete"
    return rows


def _paperops_strategy_inception_dates(
    *,
    expected_strategy_ids: set[str],
    active_series: Mapping[str, tuple[str, str, str]],
    state_dir: Path,
) -> tuple[
    dict[PaperSeriesIdentity, str],
    dict[str, str],
    tuple[Path, Path],
]:
    """Resolve exact-lineage coverage inception dates from canonical registry truth.

    A series can make official claims only after both its immutable strategy
    semantics and its execution policy are active.  The later of those two
    canonical dates wins.  Every lookup is bound to the full active identity;
    strategy IDs alone never grant an inception exemption.
    """

    semantics_path = state_dir / "strategy_semantics_manifest.json"
    policy_path = state_dir / "execution_policy_manifest.json"
    manifest_strategies, semantics_error = _manifest_mapping(
        semantics_path,
        field="strategies",
        schema_version="v2.strategy_semantics_manifest.v1",
        artifact="strategy semantics manifest",
    )
    manifest_policies, policy_error = _manifest_mapping(
        policy_path,
        field="policies",
        schema_version="v2.paper_execution_policy_manifest.v1",
        artifact="execution policy manifest",
    )

    inception_dates: dict[PaperSeriesIdentity, str] = {}
    issues: dict[str, str] = {}
    for strategy_id in sorted(expected_strategy_ids):
        lineage = active_series.get(strategy_id)
        if lineage is None:
            issues[strategy_id] = "exact active registry lineage is missing"
            continue
        identity = (strategy_id, *lineage)
        exact_key = _paper_series_key(identity)
        strategy_version, policy_version, semantics_fingerprint = lineage

        manifest_key = f"{strategy_id}@{strategy_version}"
        manifest_row = (
            manifest_strategies.get(manifest_key)
            if manifest_strategies is not None
            else None
        )
        if not isinstance(manifest_row, dict):
            issues[exact_key] = semantics_error or (
                f"strategy semantics entry {manifest_key} is missing"
            )
            continue
        if str(manifest_row.get("fingerprint") or "") != semantics_fingerprint:
            issues[exact_key] = (
                f"strategy semantics entry {manifest_key} does not match active fingerprint"
            )
            continue
        configuration = manifest_row.get("configuration")
        if isinstance(configuration, dict) and (
            str(configuration.get("strategy_id") or "") != strategy_id
            or str(configuration.get("strategy_version") or "") != strategy_version
        ):
            issues[exact_key] = (
                f"strategy semantics entry {manifest_key} configuration does not match"
            )
            continue
        policy_row = (
            manifest_policies.get(policy_version)
            if manifest_policies is not None
            else None
        )
        if not isinstance(policy_row, dict):
            issues[exact_key] = policy_error or (
                f"execution policy entry {policy_version} is missing"
            )
            continue
        strategy_inception = _registration_coverage_date(
            manifest_row.get("registered_at"),
            explicit_inception=manifest_row.get("coverage_inception_date"),
            activation_policy=manifest_row.get("activation_policy"),
        )
        if strategy_inception is None:
            issues[exact_key] = (
                f"strategy semantics entry {manifest_key} has invalid activation lineage"
            )
            continue
        policy_inception = _registration_coverage_date(
            policy_row.get("registered_at"),
            explicit_inception=policy_row.get("coverage_inception_date"),
            activation_policy=policy_row.get("activation_policy"),
        )
        if policy_inception is None:
            issues[exact_key] = (
                f"execution policy entry {policy_version} has invalid activation lineage"
            )
            continue
        inception_dates[identity] = max(strategy_inception, policy_inception)
    return inception_dates, issues, (semantics_path, policy_path)


def _manifest_mapping(
    path: Path,
    *,
    field: str,
    schema_version: str,
    artifact: str,
) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{artifact} unavailable: {exc}"
    if not isinstance(payload, dict):
        return None, f"{artifact} is not an object"
    if payload.get("schema_version") != schema_version:
        return None, f"{artifact} schema is unsupported"
    mapping = payload.get(field)
    if not isinstance(mapping, dict):
        return None, f"{artifact} {field} is not an object"
    return mapping, None


def _registration_coverage_date(
    value: object,
    *,
    explicit_inception: object = None,
    activation_policy: object = None,
) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        registered_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if registered_at.tzinfo is None or registered_at.utcoffset() is None:
        return None
    raw_policy = str(activation_policy or "").strip()
    try:
        expected_date = registration_coverage_inception_date(
            registered_at,
            raw_policy or _LEGACY_ACTIVATION_POLICY,
        )
    except ValueError:
        return None
    expected = expected_date.isoformat()
    explicit = str(explicit_inception or "").strip()
    if not explicit:
        return expected
    try:
        stored = date.fromisoformat(explicit).isoformat()
    except ValueError:
        return None
    return expected if stored == expected else None


def _paper_row_identity(row: Mapping[str, Any]) -> PaperSeriesIdentity:
    return (
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_version") or ""),
        str(row.get("execution_policy_version") or ""),
        str(row.get("strategy_semantics_fingerprint") or ""),
    )


def _paper_series_key(identity: PaperSeriesIdentity) -> str:
    return "|".join(identity)


def _paperops_coverage_dates(
    rows: list[dict[str, Any]],
    *,
    start: str | None,
    end: str | None,
    warnings: list[str],
) -> list[str]:
    """Enumerate internal/explicit retained sessions without claiming future days.

    The latest observed exact forward row is the safe upper bound.  An explicit
    report start may extend the lower bound backwards; an explicit end can only
    narrow the retained range.  This exposes a completely absent market session
    between retained endpoints instead of inspecting observed dates alone.
    """

    observed = sorted({str(row["date"]) for row in rows})
    if not observed:
        return []
    try:
        lower = date.fromisoformat(_date_part(start) or observed[0])
        requested_end = date.fromisoformat(_date_part(end) or observed[-1])
        upper = min(requested_end, date.fromisoformat(observed[-1]))
    except ValueError:
        return observed
    if lower > upper:
        return []
    sessions: list[str] = []
    current = lower
    try:
        while current <= upper:
            if market_session(current).is_trading_day:
                sessions.append(current.isoformat())
            current += timedelta(days=1)
    except MarketCalendarCoverageError as exc:
        warnings.append(
            "PaperOps whole-session coverage could not be enumerated outside the "
            f"published market calendar: {exc}"
        )
        return observed
    return sessions


def _market_date_where(
    *, start: str | None, end: str | None
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    parameters: list[str] = []
    if start:
        clauses.append("market_date >= ?")
        parameters.append(_date_part(start) or "")
    if end:
        clauses.append("market_date <= ?")
        parameters.append(_date_part(end) or "")
    return (" WHERE " + " AND ".join(clauses) if clauses else "", parameters)


def _sqlite_table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _alpha_trade_evidence(
    rows: Iterable[Mapping[str, Any]],
    *,
    official_signal_ids: set[str],
) -> dict[str, dict[tuple[str, ...], dict[str, float | int]]]:
    all_groups: dict[tuple[str, ...], dict[str, float | int]] = {}
    cohort_groups: dict[tuple[str, ...], dict[str, float | int]] = {}
    official_groups: dict[tuple[str, ...], dict[str, float | int]] = {}
    for raw in rows:
        row = dict(raw)
        base_key = (
            str(row.get("market_date") or "")[:10],
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or ""),
        )
        cohort_key = (*base_key, str(row.get("cohort") or ""))
        targets = [(all_groups, base_key), (cohort_groups, cohort_key)]
        if str(row.get("signal_id") or "") in official_signal_ids:
            targets.append((official_groups, base_key))
        for groups, group_key in targets:
            group = groups.setdefault(
                group_key,
                {"allocated_notional": 0.0, "realized_net_pnl": 0.0, "count": 0},
            )
            group["allocated_notional"] = round(
                float(group["allocated_notional"]) + float(row["notional"]), 10
            )
            group["realized_net_pnl"] = round(
                float(group["realized_net_pnl"]) + float(row["net_pnl"]), 10
            )
            group["count"] = int(group["count"]) + 1
    return {
        "all": all_groups,
        "cohort": cohort_groups,
        "official": official_groups,
    }


def _alpha_benchmark_evidence(
    rows: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
) -> tuple[dict[str, dict[str, Any]], int]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    issues = 0
    for raw in rows:
        row = dict(raw)
        market_date = str(row.get("market_date") or "")[:10]
        open_price = _optional_float(row.get("open_price"))
        close_price = _optional_float(row.get("close_price"))
        if not market_date or open_price is None or open_price <= 0 or close_price is None:
            issues += 1
            continue
        grouped[market_date].append(
            {
                "benchmark_id": str(row.get("benchmark_id") or f"{symbol}:{market_date}"),
                "return_pct": round(((close_price / open_price) - 1.0) * 100.0, 10),
                "source": str(row.get("source") or ""),
                "source_quality": str(row.get("source_quality") or ""),
                "observed_at": str(row.get("observed_at") or ""),
            }
        )

    evidence: dict[str, dict[str, Any]] = {}
    for market_date, candidates in grouped.items():
        distinct_returns = {float(row["return_pct"]) for row in candidates}
        if len(distinct_returns) != 1:
            issues += 1
            continue
        evidence[market_date] = sorted(
            candidates,
            key=lambda row: (str(row["observed_at"]), str(row["benchmark_id"])),
        )[-1]
    return evidence, issues


def _paperops_comparator_evidence(
    rows: Iterable[Mapping[str, Any]],
    *,
    start: str | None,
    end: str | None,
    warnings: list[str],
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, int], int]:
    ids = {
        PAPEROPS_BENCHMARK_ID: "benchmark",
        CASH_BASELINE_ID: "cash",
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    counts = {"benchmark": 0, "cash": 0}
    for raw in rows:
        row = dict(raw)
        if str(row.get("mode") or "").strip().lower() != "forward":
            continue
        market_date = str(row.get("date") or "")[:10]
        if not _in_range(market_date, start=start, end=end):
            continue
        comparator_id = str(row.get("strategy_id") or "").strip()
        kind = ids.get(comparator_id)
        if kind is None:
            continue
        grouped[(kind, market_date)].append(row)
        counts[kind] += 1

    evidence: dict[str, dict[str, dict[str, Any]]] = {
        "benchmark": {},
        "cash": {},
    }
    issues = 0
    for (kind, market_date), matches in sorted(grouped.items()):
        if len(matches) != 1:
            issues += 1
            warnings.append(
                f"Duplicate forward PaperOps {kind} comparator row: {market_date}"
            )
            continue
        row = matches[0]
        daily = _fraction_to_pct(_optional_float(row.get("daily_return_pct")))
        cumulative = _fraction_to_pct(_optional_float(row.get("cumulative_return_pct")))
        if daily is None or cumulative is None:
            issues += 1
            warnings.append(
                f"PaperOps {kind} comparator return is missing: {market_date}"
            )
            continue
        evidence[kind][market_date] = {
            "daily_return_pct": daily,
            "cumulative_return_pct": cumulative,
            "source": (
                f"{PAPEROPS_SOURCE}:{str(row.get('run_id') or market_date)}"
            ),
        }
    return evidence, counts, issues


def _comparison_coverage_status(rows: Iterable[Mapping[str, Any]], field: str) -> str:
    observed = [row for row in rows if row.get("return_observed")]
    if not observed:
        return "not_applicable_no_return_observations"
    available = sum(_optional_float(row.get(field)) is not None for row in observed)
    if available == len(observed):
        return "complete"
    return "partial" if available else "missing"


def _build_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        group_key = (
            str(row["source_system"]),
            str(row["horizon"]),
            str(row["mode"]),
            str(row["strategy_id"]),
            str(row["strategy_version"]),
            str(row["execution_policy_version"]),
            str(row["strategy_status"]),
            str(row["cohort"]),
        )
        groups[group_key].append(row)

    summaries: list[dict[str, Any]] = []
    for key in sorted(groups):
        matches = sorted(groups[key], key=lambda row: str(row["date"]))
        observed_rows = [
            row for row in matches if row.get("normalized_daily_return_pct") is not None
        ]
        observed = [float(row["normalized_daily_return_pct"]) for row in observed_rows]
        if key[1] == PAPEROPS_HORIZON:
            latest_source_cumulative = next(
                (
                    row["cumulative_return_source_value"]
                    for row in reversed(matches)
                    if row.get("cumulative_return_source_value") is not None
                ),
                None,
            )
            normalized_cumulative = _fraction_to_pct(
                _optional_float(latest_source_cumulative)
            )
            cumulative_semantics = "latest_source_strategy_account_cumulative_return"
            total_allocated_notional = None
            total_realized_net_pnl = None
            allocation_evidence_missing_count = None
            weighted_realized_return = None
            hypothetical_compounded = None
            latest = matches[-1]
            benchmark_cumulative = _optional_float(
                latest.get("benchmark_cumulative_return_pct")
            )
            benchmark_semantics = (
                "latest_same_horizon_forward_paper_comparator_cumulative_return"
                if benchmark_cumulative is not None
                else "N/A_missing_same_horizon_forward_paper_comparator"
            )
            cash_cumulative = _optional_float(latest.get("cash_cumulative_return_pct"))
        else:
            latest_source_cumulative = None
            allocation_evidence_missing_count = sum(
                1
                for row in observed_rows
                if _optional_float(row.get("allocated_notional")) is None
                or float(row["allocated_notional"]) <= 0
                or _optional_float(row.get("realized_net_pnl")) is None
            )
            allocation_complete = bool(observed_rows) and not allocation_evidence_missing_count
            if allocation_complete:
                total_allocated_notional = round(
                    sum(float(row["allocated_notional"]) for row in observed_rows), 10
                )
                total_realized_net_pnl = round(
                    sum(float(row["realized_net_pnl"]) for row in observed_rows), 10
                )
                weighted_realized_return = round(
                    (total_realized_net_pnl / total_allocated_notional) * 100.0,
                    10,
                )
            else:
                total_allocated_notional = None
                total_realized_net_pnl = None
                weighted_realized_return = None
            normalized_cumulative = weighted_realized_return
            hypothetical_compounded = _compound_percent_points(observed)
            cumulative_semantics = (
                "canonical_sum_realized_net_pnl_divided_by_sum_allocated_notional"
                if normalized_cumulative is not None
                else "N/A_missing_canonical_allocated_notional_or_realized_pnl"
            )
            benchmark_complete = allocation_complete and all(
                _optional_float(row.get("benchmark_return_pct")) is not None
                for row in observed_rows
            )
            benchmark_cumulative = (
                round(
                    sum(
                        float(row["allocated_notional"])
                        * float(row["benchmark_return_pct"])
                        for row in observed_rows
                    )
                    / float(total_allocated_notional),
                    10,
                )
                if benchmark_complete and total_allocated_notional
                else None
            )
            benchmark_semantics = (
                "allocated_notional_weighted_same_day_open_to_close_market_context"
                if benchmark_cumulative is not None
                else "N/A_missing_same_day_benchmark_or_allocation_evidence"
            )
            cash_cumulative = 0.0 if normalized_cumulative is not None else None
        benchmark_observation_count = sum(
            1
            for row in observed_rows
            if _optional_float(row.get("benchmark_return_pct")) is not None
        )
        benchmark_missing_count = len(observed_rows) - benchmark_observation_count
        benchmark_id = _summary_benchmark_id(matches)
        summaries.append(
            {
                "source_system": key[0],
                "horizon": key[1],
                "mode": key[2],
                "strategy_id": key[3],
                "strategy_version": key[4],
                "execution_policy_version": key[5],
                "strategy_status": key[6],
                "cohort": key[7],
                "first_date": matches[0]["date"],
                "last_date": matches[-1]["date"],
                "daily_row_count": len(matches),
                "return_observation_count": len(observed),
                "missing_return_count": len(matches) - len(observed),
                "trades_opened": _sum_int(matches, "trades_opened"),
                "trades_closed": _sum_int(matches, "trades_closed"),
                "wins": _sum_int(matches, "wins"),
                "losses": _sum_int(matches, "losses"),
                "flats": _sum_int(matches, "flats"),
                "unresolved_count": _sum_int(matches, "unresolved_count"),
                "total_allocated_notional": total_allocated_notional,
                "total_realized_net_pnl": total_realized_net_pnl,
                "allocation_evidence_missing_count": allocation_evidence_missing_count,
                "weighted_realized_return_pct": weighted_realized_return,
                "hypothetical_compounded_daily_return_pct": hypothetical_compounded,
                "latest_cumulative_return_source_value": latest_source_cumulative,
                "source_return_scale": matches[-1]["source_return_scale"],
                "normalized_cumulative_return_pct": normalized_cumulative,
                "cumulative_return_semantics": cumulative_semantics,
                "benchmark_id": benchmark_id,
                "benchmark_observation_count": benchmark_observation_count,
                "benchmark_missing_count": benchmark_missing_count,
                "normalized_benchmark_cumulative_return_pct": benchmark_cumulative,
                "normalized_excess_return_vs_benchmark_pct": _subtract_optional(
                    normalized_cumulative, benchmark_cumulative
                ),
                "benchmark_cumulative_semantics": benchmark_semantics,
                "cash_baseline_id": CASH_BASELINE_ID,
                "normalized_cash_cumulative_return_pct": cash_cumulative,
                "normalized_excess_return_vs_cash_pct": _subtract_optional(
                    normalized_cumulative, cash_cumulative
                ),
            }
        )
    return summaries


def _write_artifacts(output_dir: Path, payload: Mapping[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "strategy_fleet_report.json"
    daily_path = output_dir / "strategy_fleet_daily.csv"
    summary_path = output_dir / "strategy_fleet_summaries.csv"
    markdown_path = output_dir / "strategy_fleet_report.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_csv(daily_path, payload["daily_rows"], _DAILY_FIELDS)
    _write_csv(summary_path, payload["strategy_summaries"], _SUMMARY_FIELDS)
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return {
        "json": str(json_path),
        "daily_csv": str(daily_path),
        "summary_csv": str(summary_path),
        "markdown": str(markdown_path),
    }


def _write_csv(path: Path, rows: Any, fields: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Dawnstrike Strategy Fleet Report",
        "",
        f"- Status: `{payload['status']}`",
        "- Cross-horizon return aggregation: **prohibited**",
        "- AlphaOps no-entry returns remain `N/A`; they are not recorded as 0%.",
        "- AlphaOps cumulative return is realized P&L / allocated notional; daily "
        "compounding is hypothetical and shown separately.",
        "- PaperOps source returns are fractional equity returns and are normalized explicitly.",
        "- Missing same-horizon benchmark evidence remains `N/A`.",
        "- Cash comparison is the catalog no-trade 0% policy baseline, not a cash yield.",
        "",
    ]
    warnings = list(payload["warnings"])
    if warnings:
        lines.extend(["## Evidence warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    for horizon in (ALPHAOPS_HORIZON, PAPEROPS_HORIZON):
        summaries = [row for row in payload["strategy_summaries"] if row["horizon"] == horizon]
        lines.extend(
            [
                f"## {horizon}",
                "",
                "| Strategy | Cohort | Days | Return observations | Missing | "
                "Cumulative normalized | Hypothetical daily compound | Benchmark excess | "
                "Cash excess |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        if summaries:
            for row in summaries:
                cumulative = _display(row["normalized_cumulative_return_pct"], suffix="%")
                hypothetical = _display(
                    row["hypothetical_compounded_daily_return_pct"], suffix="%"
                )
                benchmark_excess = _display(
                    row["normalized_excess_return_vs_benchmark_pct"], suffix="%"
                )
                cash_excess = _display(
                    row["normalized_excess_return_vs_cash_pct"], suffix="%"
                )
                lines.append(
                    f"| {row['strategy_id']}@{row['strategy_version']} "
                    f"[{row['execution_policy_version']}] | {row['cohort']} | "
                    f"{row['daily_row_count']} | "
                    f"{row['return_observation_count']} | {row['missing_return_count']} | "
                    f"{cumulative} | {hypothetical} | {benchmark_excess} | "
                    f"{cash_excess} |"
                )
        else:
            lines.append("| N/A | N/A | 0 | 0 | 0 | N/A | N/A | N/A | N/A |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _display(value: Any, *, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.4f}{suffix}"


def _date_part(value: str | None) -> str | None:
    return str(value)[:10] if value else None


def _in_range(value: str, *, start: str | None, end: str | None) -> bool:
    lower = _date_part(start)
    upper = _date_part(end)
    return not ((lower and value < lower) or (upper and value > upper))


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    number = _optional_float(value)
    return int(number) if number is not None else None


def _fraction_to_pct(value: float | None) -> float | None:
    return round(value * 100.0, 10) if value is not None else None


def _compound_percent_points(values: Iterable[float]) -> float | None:
    factor = 1.0
    count = 0
    for value in values:
        factor *= 1.0 + (value / 100.0)
        count += 1
    return round((factor - 1.0) * 100.0, 10) if count else None


def _subtract_optional(left: Any, right: Any) -> float | None:
    left_value = _optional_float(left)
    right_value = _optional_float(right)
    if left_value is None or right_value is None:
        return None
    return round(left_value - right_value, 10)


def _summary_benchmark_id(rows: Iterable[Mapping[str, Any]]) -> str | None:
    matches = list(rows)
    ids = {
        str(row.get("benchmark_id") or "")
        for row in matches
        if str(row.get("benchmark_id") or "")
    }
    if not ids:
        return None
    if matches and str(matches[0].get("horizon")) == ALPHAOPS_HORIZON:
        symbols = {benchmark_id.split(":", 1)[0] for benchmark_id in ids}
        return next(iter(symbols)) if len(symbols) == 1 else None
    return next(iter(ids)) if len(ids) == 1 else None


def _sum_int(rows: Iterable[Mapping[str, Any]], field: str) -> int:
    return sum(int(row[field]) for row in rows if row.get(field) is not None)
