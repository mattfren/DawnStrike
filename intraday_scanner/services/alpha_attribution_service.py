"""Causal AlphaOps attribution from sourced, reconciled paper evidence.

The report is descriptive research evidence. It never scores a live order,
changes a strategy, or converts missing outcomes into zero-return observations.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from intraday_scanner.performance.paper_ops import load_paper_ops
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

ALPHA_STRATEGIES = frozenset({"alphaops_v4", "alphaops_v5"})
OFFICIAL_COHORT = "official_telegram"
ATTRIBUTION_VERSION = "alphaops-causal-attribution-v1"
CROSS_VERSION_ATTRIBUTION_VERSION = "alphaops-cross-version-attribution-v1"


def generate_alpha_attribution_report(
    *,
    db_path: str | Path,
    out_dir: str | Path = "outputs/alpha_attribution",
    start: str | None = None,
    end: str | None = None,
    paper_ops_root: str | Path | None = None,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    signals = store.load_historical_signals(start=start, end=end, limit=50_000)
    selections = [
        row
        for row in store.load_signal_selections(limit=50_000)
        if str(row.get("strategy_id") or "") in ALPHA_STRATEGIES
        and _within(str(row.get("selected_at") or "")[:10], start, end)
    ]
    evaluations = [
        row
        for row in store.load_strategy_evaluations(
            start=start,
            end=end,
            limit=50_000,
        )
        if str(row.get("strategy_id") or "") in ALPHA_STRATEGIES
    ]
    trades = [
        row
        for row in store.load_strategy_paper_trades(
            start=start,
            end=end,
            limit=50_000,
        )
        if str(row.get("strategy_id") or "") in ALPHA_STRATEGIES
    ]
    attempts = [
        row
        for row in store.load_outcome_capture_attempts(limit=50_000)
        if _within(str(row.get("market_date") or "")[:10], start, end)
    ]
    intents = [
        row
        for row in store.load_trade_intents(limit=50_000)
        if _within(str(row.get("market_date") or "")[:10], start, end)
    ]
    v6_decisions = [
        row
        for row in store.load_alpha_v6_decisions(limit=50_000)
        if _within(str(row.get("market_date") or "")[:10], start, end)
    ]
    v6_outcomes = [
        row
        for row in store.load_alpha_v6_outcomes(limit=50_000)
        if _within(str(row.get("market_date") or "")[:10], start, end)
    ]
    paper_ops = load_paper_ops(paper_ops_root, market_date=end)
    report = build_alpha_attribution_report(
        signals=signals,
        selections=selections,
        evaluations=evaluations,
        trades=trades,
        attempts=attempts,
        intents=intents,
        v6_decisions=v6_decisions,
        v6_outcomes=v6_outcomes,
        paper_ops_rows=list(paper_ops.get("rows") or []),
        paper_ops_issues=list(paper_ops.get("issues") or []),
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    _atomic_json(output / "alpha_attribution.json", report)
    _atomic_text(output / "alpha_attribution.md", _markdown(report))
    return {
        **report,
        "artifacts": {
            "json": str(output / "alpha_attribution.json"),
            "markdown": str(output / "alpha_attribution.md"),
        },
    }


def build_alpha_attribution_report(
    *,
    signals: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    generated_at: str,
    v6_decisions: list[dict[str, Any]] | None = None,
    v6_outcomes: list[dict[str, Any]] | None = None,
    paper_ops_rows: list[dict[str, Any]] | None = None,
    paper_ops_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    signal_by_id = {
        str(row.get("signal_id") or ""): row for row in signals if str(row.get("signal_id") or "")
    }
    selection_by_signal = {
        str(row.get("signal_id") or ""): row
        for row in selections
        if str(row.get("signal_id") or "")
    }
    enriched = [_enriched_trade(row, signal_by_id, selection_by_signal) for row in trades]
    official = [row for row in enriched if str(row.get("cohort") or "") == OFFICIAL_COHORT]
    dates = sorted(
        {
            _row_date(row)
            for row in [*selections, *evaluations, *trades, *attempts]
            if _row_date(row)
        }
    )
    daily = [
        _daily_row(
            day,
            selections=selections,
            evaluations=evaluations,
            trades=enriched,
            attempts=attempts,
        )
        for day in dates
    ]
    buckets = {
        "cohort": _bucket_summaries(enriched, "cohort"),
        "strategy": _bucket_summaries(enriched, "strategy_id"),
        "selection_decision": _bucket_summaries(enriched, "selection_decision"),
        "setup": _bucket_summaries(enriched, "setup_key"),
        "gap_bucket": _bucket_summaries(enriched, "gap_bucket"),
        "catalyst_class": _bucket_summaries(enriched, "catalyst_class"),
        "float_bucket": _bucket_summaries(enriched, "float_bucket"),
        "liquidity_bucket": _bucket_summaries(enriched, "liquidity_bucket"),
        "market_regime": _bucket_summaries(enriched, "market_regime"),
        "sector_regime": _bucket_summaries(enriched, "sector_regime"),
        "source_confidence_bucket": _bucket_summaries(
            enriched,
            "source_confidence_bucket",
        ),
    }
    terminal_missing = [
        row for row in attempts if str(row.get("status") or "") == "terminal_missing"
    ]
    report: dict[str, Any] = {
        "schema_version": "dawnstrike.alpha_attribution.v1",
        "attribution_version": ATTRIBUTION_VERSION,
        "generated_at": generated_at,
        "status": "complete" if evaluations or trades or attempts else "no_evidence",
        "evidence_cutoff": max(dates, default=None),
        "official": _trade_summary(official),
        "all_research_cohorts": _trade_summary(enriched),
        "daily": daily,
        "buckets": buckets,
        "loss_concentration": _concentration(official),
        "symbol_concentration": _symbol_concentration(official),
        "decision_gate_effectiveness": _gate_effectiveness(
            evaluations,
            intents,
            trades,
        ),
        "entry_failure_modes": _entry_failure_modes(evaluations, intents),
        "exit_modes": _exit_modes(official),
        "cross_version_attribution": _cross_version_attribution(
            signals=signals,
            selections=selections,
            trades=enriched,
            v6_decisions=list(v6_decisions or []),
            v6_outcomes=list(v6_outcomes or []),
            paper_ops_rows=list(paper_ops_rows or []),
            paper_ops_issues=list(paper_ops_issues or []),
        ),
        "outcome_coverage": {
            "attempt_count": len(attempts),
            "resolved_count": len(attempts) - len(terminal_missing),
            "terminal_missing_count": len(terminal_missing),
            "coverage_pct": (
                round(
                    ((len(attempts) - len(terminal_missing)) / len(attempts)) * 100.0,
                    4,
                )
                if attempts
                else None
            ),
            "missing_is_zero": False,
        },
        "sample_warning": (
            "insufficient_forward_sample" if len(official) < 100 else "forward_sample_size_gate_met"
        ),
        "promotion_status": "operator_review_required_not_promoted",
        "research_only": True,
        "broker_execution_enabled": False,
        "personalized_advice": False,
        "limitations": [
            "Attribution is observational and does not prove a strategy is profitable.",
            "Expected metrics remain null where the original signal stored no expectation.",
            "Missing outcomes and no-trade decisions are excluded from return denominators.",
            "Promotion still requires the separately versioned strict forward-evidence gate.",
        ],
    }
    report["input_hash_sha256"] = _hash(
        {
            "signals": signals,
            "selections": selections,
            "evaluations": evaluations,
            "trades": trades,
            "attempts": attempts,
            "intents": intents,
            "v6_decisions": v6_decisions or [],
            "v6_outcomes": v6_outcomes or [],
            "paper_ops_rows": paper_ops_rows or [],
            "paper_ops_issues": paper_ops_issues or [],
        }
    )
    report["payload_hash_sha256"] = _hash(report)
    return report


def _cross_version_attribution(
    *,
    signals: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    v6_decisions: list[dict[str, Any]],
    v6_outcomes: list[dict[str, Any]],
    paper_ops_rows: list[dict[str, Any]],
    paper_ops_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare real evidence streams without treating their semantics as equal.

    V4/V5 observations are per reconciled paper trade, V6 observations are
    decision/outcome pairs (including sampled policy rejects), and PaperOps is
    a daily aggregate.  The report labels that difference rather than pooling
    the values into one synthetic return series.
    """

    signal_by_id = {
        str(row.get("signal_id") or ""): row for row in signals if str(row.get("signal_id") or "")
    }
    selection_by_signal = {
        str(row.get("signal_id") or ""): row
        for row in selections
        if str(row.get("signal_id") or "")
    }
    observations = [
        _historical_cross_observation(
            trade,
            signal=signal_by_id.get(str(trade.get("signal_id") or ""), {}),
            selection=selection_by_signal.get(str(trade.get("signal_id") or ""), {}),
        )
        for trade in trades
        if str(trade.get("strategy_id") or "") in ALPHA_STRATEGIES
    ]
    outcome_by_decision = {
        str(row.get("decision_id") or ""): row
        for row in v6_outcomes
        if str(row.get("decision_id") or "")
    }
    observations.extend(
        _v6_cross_observation(
            decision,
            outcome=outcome_by_decision.get(str(decision.get("decision_id") or "")),
        )
        for decision in v6_decisions
    )
    observations.extend(_paper_ops_cross_observation(row) for row in paper_ops_rows)
    dimensions = (
        "evidence_stream",
        "source_data_quality",
        "universe_identity_corporate_action",
        "selection_quality",
        "sampled_reject_regret",
        "catalyst_quality",
        "regime_quality",
        "liquidity_quality",
        "liquidity_capacity",
        "entry_timing",
        "stop_invalidation_geometry",
        "target_exit_logic",
        "exit_path",
        "sizing_concentration",
        "concentration_key",
        "tail_loss",
        "outcome_reconciliation_quality",
    )
    breakdowns = {field: _cross_bucket_summaries(observations, field) for field in dimensions}
    source_failures = [
        row
        for row in breakdowns["source_data_quality"]
        if row["bucket"].startswith(("missing", "terminal", "quarantined", "failed"))
    ]
    selection_failures = [
        row
        for row in breakdowns["selection_quality"]
        if row["bucket"].startswith(("rejected", "veto", "blocked", "no_"))
    ]
    eligible = [
        row for row in observations if _number(row.get("after_cost_return_pct")) is not None
    ]
    return {
        "schema_version": CROSS_VERSION_ATTRIBUTION_VERSION,
        "status": "COMPLETE" if observations else "WAITING_FOR_EVIDENCE",
        "observation_count": len(observations),
        "return_eligible_count": len(eligible),
        "return_missing_count": len(observations) - len(eligible),
        "missing_truth_is_zero": False,
        "semantic_boundaries": {
            "ALPHAOPS_V4": "reconciled paper trades",
            "ALPHAOPS_V5": "reconciled paper trades",
            "ALPHAOPS_V6": "sourced forward shadow decision/outcome pairs",
            "ALPHAOPS_V6_SAMPLED_REJECT": "sampled policy-reject counterfactuals",
            "PAPEROPS": "daily aggregate source rows; not trade-level evidence",
        },
        "category_breakdowns": breakdowns,
        "source_data_failures": source_failures,
        "selection_failures": selection_failures,
        "unattributed_insufficient_evidence_count": sum(
            1
            for row in observations
            if row.get("outcome_reconciliation_quality") == "unattributed_insufficient_evidence"
        ),
        "paper_ops_issue_count": len(paper_ops_issues),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _historical_cross_observation(
    trade: dict[str, Any],
    *,
    signal: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    raw = signal.get("raw_payload_json")
    facts = dict(raw) if isinstance(raw, dict) else {}
    merged = {**facts, **signal, **trade}
    source_value = str(merged.get("outcome_source") or merged.get("source") or "").strip()
    return {
        "observation_id": str(trade.get("trade_id") or trade.get("signal_id") or "unknown"),
        "market_date": _row_date(trade),
        "evidence_stream": _historical_stream(trade),
        "source_data_quality": "source_recorded" if source_value else "missing_source",
        "universe_identity_corporate_action": _universe_identity_category(merged),
        "selection_quality": _selection_bucket(selection, trade),
        "sampled_reject_regret": "not_applicable_not_sampled_reject",
        "catalyst_quality": _category(
            merged.get("catalyst_category") or merged.get("catalyst_class"),
            missing="missing_catalyst",
        ),
        "regime_quality": _regime_category(merged),
        "liquidity_quality": _liquidity_bucket(
            _first_number(merged, "dollar_volume", "premarket_dollar_volume")
        ),
        "liquidity_capacity": _liquidity_capacity_category(merged),
        "entry_timing": _category(
            merged.get("entry_time") or merged.get("entry_trigger_type"),
            missing="missing_entry_timing",
        ),
        "stop_invalidation_geometry": _stop_geometry_category(merged),
        "target_exit_logic": _target_exit_category(merged, trade),
        "exit_path": _category(trade.get("exit_reason"), missing="missing_exit_path"),
        "sizing_concentration": _sizing_category(merged),
        "concentration_key": str(trade.get("ticker") or "UNKNOWN").upper(),
        "tail_loss": _tail_loss_category(_number(trade.get("net_return_pct"))),
        "outcome_reconciliation_quality": _historical_outcome_quality(trade),
        "after_cost_return_pct": _number(trade.get("net_return_pct")),
        "benchmark_excess_return_pct": _number(trade.get("excess_return_pct")),
        "mfe_pct": _first_number(merged, "mfe_pct", "high_return_pct"),
        "mae_pct": _first_number(merged, "mae_pct", "low_drawdown_pct"),
        "uncertainty_pct": _first_number(merged, "uncertainty_pct", "prediction_uncertainty_pct"),
        "activation_status": _category(merged.get("activation_status"), missing="not_recorded"),
        "outcome_path": _category(trade.get("exit_reason"), missing="missing_exit_path"),
        "source_lineage_present": bool(
            merged.get("source_lineage_hash_sha256") or merged.get("source_ref") or source_value
        ),
        "net_return_pct": _number(trade.get("net_return_pct")),
        "net_pnl": _number(trade.get("net_pnl")),
        "coverage_status": "SOURCED_COMPLETE",
    }


def _v6_cross_observation(
    decision: dict[str, Any], *, outcome: dict[str, Any] | None
) -> dict[str, Any]:
    raw_facts = decision.get("raw_facts")
    facts = dict(raw_facts) if isinstance(raw_facts, dict) else {}
    features = decision.get("feature_vector")
    feature_data = dict(features) if isinstance(features, dict) else {}
    feature_json = feature_data.get("feature_json")
    feature_values = dict(feature_json) if isinstance(feature_json, dict) else {}
    source_summary = decision.get("source_summary")
    source = dict(source_summary) if isinstance(source_summary, dict) else {}
    rejection = decision.get("rejected_sampling")
    sampled_reject = (
        str(decision.get("action") or "") == "SHADOW_REJECTED_POLICY"
        and isinstance(rejection, dict)
        and rejection.get("included") is True
    )
    outcome_data = dict(outcome or {})
    outcome_status = str(outcome_data.get("outcome_status") or "").upper()
    if outcome_status == "TERMINAL_MISSING":
        source_quality = "terminal_missing_source_outcome"
    elif str(source.get("status") or "").lower() in {"success", "complete"}:
        source_quality = "source_complete"
    else:
        source_quality = _category(source.get("status"), missing="missing_source")
    liquidity = feature_values.get("liquidity_execution")
    liquidity_data = dict(liquidity) if isinstance(liquidity, dict) else {}
    catalyst = feature_values.get("catalyst")
    catalyst_data = dict(catalyst) if isinstance(catalyst, dict) else {}
    universe = decision.get("universe_membership")
    universe_data = dict(universe) if isinstance(universe, dict) else {}
    uncertainty = decision.get("uncertainty")
    uncertainty_data = dict(uncertainty) if isinstance(uncertainty, dict) else {}
    vetoes = list(decision.get("safety_vetoes") or [])
    return {
        "observation_id": str(decision.get("decision_id") or "unknown"),
        "market_date": str(decision.get("market_date") or "")[:10],
        "evidence_stream": ("ALPHAOPS_V6_SAMPLED_REJECT" if sampled_reject else "ALPHAOPS_V6"),
        "source_data_quality": source_quality,
        "universe_identity_corporate_action": _v6_universe_identity_category(
            universe_data,
            facts,
        ),
        "selection_quality": (
            "veto_" + _category(vetoes[0]) if vetoes else _category(decision.get("action"))
        ),
        "sampled_reject_regret": _sampled_reject_regret(
            sampled_reject,
            outcome_data,
        ),
        "catalyst_quality": _category(
            catalyst_data.get("category")
            or catalyst_data.get("status")
            or facts.get("catalyst_category"),
            missing="missing_catalyst",
        ),
        "regime_quality": _regime_category({"market_regime": decision.get("regime_key"), **facts}),
        "liquidity_quality": _liquidity_bucket(
            _number(liquidity_data.get("premarket_dollar_volume"))
        ),
        "liquidity_capacity": _liquidity_capacity_category({**feature_values, **facts}),
        "entry_timing": _category(
            outcome_data.get("activation_status"), missing="missing_entry_timing"
        ),
        "stop_invalidation_geometry": _stop_geometry_category(
            {**facts, **decision, **outcome_data}
        ),
        "target_exit_logic": _target_exit_category(
            {**facts, **decision, **outcome_data},
            outcome_data,
        ),
        "exit_path": _category(
            outcome_data.get("first_touch") or outcome_data.get("exit_reason"),
            missing="missing_exit_path",
        ),
        "sizing_concentration": _sizing_category({**facts, **decision}),
        "concentration_key": str(decision.get("ticker") or "UNKNOWN").upper(),
        "tail_loss": _tail_loss_category(_number(outcome_data.get("net_excess_return_pct"))),
        "outcome_reconciliation_quality": _v6_outcome_quality(outcome_data),
        "after_cost_return_pct": (
            _first_number(
                outcome_data,
                "net_return_pct",
                "after_cost_return_pct",
                "net_excess_return_pct",
            )
            if outcome_data.get("learning_eligible") is True
            else None
        ),
        "benchmark_excess_return_pct": (
            _number(outcome_data.get("net_excess_return_pct"))
            if outcome_data.get("learning_eligible") is True
            else None
        ),
        "mfe_pct": _number(outcome_data.get("mfe_pct")),
        "mae_pct": _number(outcome_data.get("mae_pct")),
        "uncertainty_pct": _first_number(
            uncertainty_data,
            "uncertainty_pct",
            "interval_width_pct",
        ),
        "activation_status": _category(
            outcome_data.get("activation_status"), missing="not_recorded"
        ),
        "outcome_path": _category(
            outcome_data.get("first_touch") or outcome_data.get("exit_reason"),
            missing="missing_exit_path",
        ),
        "source_lineage_present": bool(
            decision.get("source_lineage_hash_sha256")
            or universe_data.get("source_lineage_hash_sha256")
        ),
        "net_return_pct": (
            _number(outcome_data.get("net_excess_return_pct"))
            if outcome_data.get("learning_eligible") is True
            else None
        ),
        "net_pnl": None,
        "coverage_status": outcome_status or "MISSING_OUTCOME",
    }


def _paper_ops_cross_observation(row: dict[str, Any]) -> dict[str, Any]:
    record_status = str(row.get("record_status") or "").lower()
    return {
        "observation_id": str(row.get("record_id") or "unknown"),
        "market_date": str(row.get("market_date") or row.get("date") or "")[:10],
        "evidence_stream": "PAPEROPS",
        "source_data_quality": (
            "paperops_source_complete"
            if record_status in {"accepted", "realized", "no_trade"}
            else "quarantined_paperops_source"
        ),
        "universe_identity_corporate_action": "not_recorded_aggregate",
        "selection_quality": "aggregate_daily_record",
        "sampled_reject_regret": "not_applicable_aggregate",
        "catalyst_quality": "not_recorded_aggregate",
        "regime_quality": "not_recorded_aggregate",
        "liquidity_quality": "not_recorded_aggregate",
        "liquidity_capacity": "not_recorded_aggregate",
        "entry_timing": "not_recorded_aggregate",
        "stop_invalidation_geometry": "not_recorded_aggregate",
        "target_exit_logic": "not_recorded_aggregate",
        "exit_path": "not_recorded_aggregate",
        "sizing_concentration": "not_recorded_aggregate",
        "concentration_key": str(row.get("strategy_id") or "UNKNOWN").upper(),
        "tail_loss": "not_recorded_aggregate",
        "outcome_reconciliation_quality": (
            "paperops_reconciled"
            if record_status in {"accepted", "realized", "no_trade"}
            else "unattributed_insufficient_evidence"
        ),
        "after_cost_return_pct": _number(row.get("return_pct")),
        "benchmark_excess_return_pct": _number(row.get("excess_return_pct")),
        "mfe_pct": None,
        "mae_pct": None,
        "uncertainty_pct": None,
        "activation_status": "not_recorded_aggregate",
        "outcome_path": "not_recorded_aggregate",
        "source_lineage_present": bool(row.get("source_hash_sha256")),
        "net_return_pct": _number(row.get("return_pct")),
        # PaperOps reports cents while AlphaOps paper trades report their own
        # notional semantics. Do not pool non-comparable P&L units.
        "net_pnl": None,
        "native_net_pnl_cents": _number(row.get("net_pnl_cents")),
        "coverage_status": record_status.upper() or "MISSING_RECORD_STATUS",
    }


def _cross_bucket_summaries(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(field) or "unknown"), []).append(row)
    return [{"bucket": key, **_cross_summary(groups[key])} for key in sorted(groups)]


def _cross_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = _numbers(rows, "after_cost_return_pct")
    benchmark_excess = _numbers(rows, "benchmark_excess_return_pct")
    pnl = _numbers(rows, "net_pnl")
    mfe = _numbers(rows, "mfe_pct")
    mae = _numbers(rows, "mae_pct")
    uncertainty = _numbers(rows, "uncertainty_pct")
    excluded = [row for row in rows if _cross_row_is_excluded(row)]
    missing = [
        row
        for row in rows
        if _number(row.get("after_cost_return_pct")) is None and row not in excluded
    ]
    activation = Counter(str(row.get("activation_status") or "not_recorded") for row in rows)
    paths = Counter(str(row.get("outcome_path") or "missing") for row in rows)
    lineage_count = sum(row.get("source_lineage_present") is True for row in rows)
    coverage_denominator = len(rows) - len(excluded)
    return {
        "observation_count": len(rows),
        "return_eligible_count": len(returns),
        "return_missing_count": len(missing),
        "return_excluded_count": len(excluded),
        "coverage_pct": (
            round((len(returns) / coverage_denominator) * 100.0, 4)
            if coverage_denominator
            else None
        ),
        "mean_after_cost_return_pct": round(mean(returns), 4) if returns else None,
        "mean_benchmark_excess_return_pct": (
            round(mean(benchmark_excess), 4) if benchmark_excess else None
        ),
        "mean_mfe_pct": round(mean(mfe), 4) if mfe else None,
        "mean_mae_pct": round(mean(mae), 4) if mae else None,
        "mean_uncertainty_pct": round(mean(uncertainty), 4) if uncertainty else None,
        "activation_counts": dict(sorted(activation.items())),
        "stop_target_close_path_counts": dict(sorted(paths.items())),
        "source_lineage_coverage_pct": (
            round((lineage_count / len(rows)) * 100.0, 4) if rows else None
        ),
        "net_pnl": round(sum(pnl), 4) if pnl else None,
        "missing_truth_is_zero": False,
    }


def _cross_row_is_excluded(row: dict[str, Any]) -> bool:
    status = str(row.get("coverage_status") or "").upper()
    return status in {
        "NOT_TRIGGERED",
        "NO_TRADE",
        "RESEARCH_ONLY_POLICY_BLOCKED",
        "QUARANTINED",
    }


def _universe_identity_category(row: dict[str, Any]) -> str:
    identity = _category(
        row.get("identity_status") or row.get("ticker_identity_status"),
        missing="missing_identity",
    )
    listing = _category(row.get("listing_status"), missing="missing_listing")
    corporate = _category(
        row.get("corporate_action_status") or row.get("corporate_action_type"),
        missing="missing_corporate_action",
    )
    if {identity, listing, corporate} == {
        "missing_identity",
        "missing_listing",
        "missing_corporate_action",
    }:
        return "unattributed_insufficient_evidence"
    return f"identity_{identity}|listing_{listing}|corporate_action_{corporate}"


def _v6_universe_identity_category(universe: dict[str, Any], facts: dict[str, Any]) -> str:
    merged = {**facts, **universe}
    membership = _category(merged.get("status"), missing="missing_membership")
    base = _universe_identity_category(merged)
    return f"membership_{membership}|{base}"


def _regime_category(row: dict[str, Any]) -> str:
    market = _category(
        row.get("market_regime") or row.get("regime_key") or row.get("regime"),
        missing="missing_market_regime",
    )
    sector = _category(row.get("sector_regime"), missing="missing_sector_regime")
    if market == "missing_market_regime" and sector == "missing_sector_regime":
        return "unattributed_insufficient_evidence"
    return f"market_{market}|sector_{sector}"


def _liquidity_capacity_category(row: dict[str, Any]) -> str:
    liquidity = _liquidity_bucket(
        _first_number(
            row,
            "dollar_volume",
            "premarket_dollar_volume",
            "avg_dollar_volume_20d",
        )
    )
    capacity = _first_number(
        row,
        "estimated_capacity_dollars",
        "capacity_dollars",
        "capacity",
    )
    capacity_bucket = (
        "missing_capacity"
        if capacity is None
        else "under_10k"
        if capacity < 10_000
        else "10k_to_100k"
        if capacity < 100_000
        else "over_100k"
    )
    return f"{liquidity}|{capacity_bucket}"


def _stop_geometry_category(row: dict[str, Any]) -> str:
    entry = _first_number(
        row,
        "entry_price",
        "entry_trigger",
        "breakout_trigger",
        "entry_watch_level",
    )
    stop = _first_number(row, "invalidation_level", "invalidation", "stop_price")
    if entry is None or stop is None or entry <= 0:
        return "unattributed_insufficient_evidence"
    distance = abs(entry - stop) / entry * 100.0
    return (
        "tight_under_3pct"
        if distance < 3.0
        else "moderate_3_to_8pct"
        if distance <= 8.0
        else "wide_over_8pct"
    )


def _target_exit_category(row: dict[str, Any], outcome: dict[str, Any]) -> str:
    target = _first_number(row, "target_1", "first_target", "target_price")
    exit_path = _category(
        outcome.get("first_touch") or outcome.get("exit_reason"),
        missing="missing_exit_path",
    )
    if target is None:
        return f"missing_target|{exit_path}"
    return f"target_recorded|{exit_path}"


def _sizing_category(row: dict[str, Any]) -> str:
    notional = _first_number(row, "notional", "notional_dollars", "position_notional")
    quantity = _first_number(row, "quantity", "shares")
    account = _category(row.get("account_id"), missing="missing_account")
    if notional is None and quantity is None:
        return f"{account}|unattributed_insufficient_evidence"
    return f"{account}|sizing_recorded"


def _tail_loss_category(value: float | None) -> str:
    if value is None:
        return "unattributed_insufficient_evidence"
    if value <= -5.0:
        return "tail_loss_at_or_below_minus_5pct"
    if value < 0:
        return "loss_above_tail_threshold"
    return "non_loss"


def _historical_outcome_quality(trade: dict[str, Any]) -> str:
    status = _category(
        trade.get("reconciliation_status") or trade.get("record_status"),
        missing="missing_reconciliation_status",
    )
    if status.startswith(("missing", "quarantined", "failed")):
        return "unattributed_insufficient_evidence"
    return f"reconciled_{status}"


def _v6_outcome_quality(outcome: dict[str, Any]) -> str:
    status = _category(outcome.get("outcome_status"), missing="missing_outcome")
    if status in {"terminal_missing", "missing_outcome"}:
        return "unattributed_insufficient_evidence"
    return f"reconciled_{status}"


def _sampled_reject_regret(sampled_reject: bool, outcome: dict[str, Any]) -> str:
    if not sampled_reject:
        return "not_applicable_not_sampled_reject"
    if str(outcome.get("outcome_status") or "").upper() == "TERMINAL_MISSING":
        return "unattributed_insufficient_evidence"
    value = _number(outcome.get("net_excess_return_pct"))
    if value is None:
        return "unattributed_insufficient_evidence"
    return "sampled_reject_positive_regret" if value > 0 else "sampled_reject_avoided_loss"


def _historical_stream(trade: dict[str, Any]) -> str:
    strategy = str(trade.get("strategy_id") or "").upper()
    return "ALPHAOPS_V4" if strategy == "ALPHAOPS_V4" else "ALPHAOPS_V5"


def _selection_bucket(selection: dict[str, Any], trade: dict[str, Any]) -> str:
    value = selection.get("decision") or trade.get("selection_decision") or "entered"
    return _category(value)


def _category(value: Any, *, missing: str = "missing") -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    return text or missing


def _daily_row(
    day: str,
    *,
    selections: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    day_selections = [row for row in selections if str(row.get("selected_at") or "")[:10] == day]
    day_evaluations = [row for row in evaluations if str(row.get("market_date") or "")[:10] == day]
    day_trades = [row for row in trades if str(row.get("market_date") or "")[:10] == day]
    day_attempts = [row for row in attempts if str(row.get("market_date") or "")[:10] == day]
    explicit_no_trade = any(
        str(row.get("decision") or "").lower() == "no_trade"
        or str(row.get("ticker") or "").upper() == "NO_TRADE"
        for row in day_selections
    )
    missing = sum(1 for row in day_attempts if str(row.get("status") or "") == "terminal_missing")
    if missing:
        status = "MISSING"
    elif day_trades:
        status = "COMPLETE"
    elif explicit_no_trade:
        status = "NO_TRADE"
    elif day_evaluations:
        status = "COMPLETE_NO_FILL"
    else:
        status = "PENDING"
    summary = _trade_summary(day_trades)
    return {
        "market_date": day,
        "status": status,
        "selection_count": len(day_selections),
        "evaluation_count": len(day_evaluations),
        "terminal_missing_count": missing,
        **summary,
    }


def _enriched_trade(
    trade: dict[str, Any],
    signal_by_id: dict[str, dict[str, Any]],
    selection_by_signal: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    signal = signal_by_id.get(str(trade.get("signal_id") or ""), {})
    selection = selection_by_signal.get(str(trade.get("signal_id") or ""), {})
    raw = signal.get("raw_payload_json")
    facts = dict(raw) if isinstance(raw, dict) else {}
    merged = {**facts, **signal}
    gap = _number(merged.get("gap_pct"))
    float_shares = _number(merged.get("float_shares"))
    liquidity = _first_number(
        merged,
        "dollar_volume",
        "premarket_dollar_volume",
    )
    confidence = _first_number(
        merged,
        "source_confidence",
        "data_confidence",
    )
    expected_hit = _first_number(
        merged,
        "expected_win_probability_pct",
        "calibrated_probability_pct",
        "predicted_probability_pct",
        "probability_pct",
    )
    if expected_hit is None:
        probability = _first_number(
            merged,
            "expected_win_probability",
            "calibrated_probability",
            "predicted_probability",
            "probability",
        )
        expected_hit = (
            probability * 100.0 if probability is not None and probability <= 1 else probability
        )
    return {
        **trade,
        "selection_decision": selection.get("decision") or "unlinked",
        "setup_key": (merged.get("setup_key") or merged.get("primary_setup") or "unknown"),
        "gap_bucket": _gap_bucket(gap),
        "catalyst_class": (
            merged.get("catalyst_category") or merged.get("catalyst_class") or "unknown"
        ),
        "float_bucket": _float_bucket(float_shares),
        "liquidity_bucket": _liquidity_bucket(liquidity),
        "market_regime": merged.get("market_regime") or merged.get("regime") or "unknown",
        "sector_regime": merged.get("sector_regime") or "unknown",
        "source_confidence_bucket": _confidence_bucket(confidence),
        "expected_hit_rate_pct": expected_hit,
        "expected_r_multiple": _first_number(
            merged,
            "expected_r_multiple",
            "actual_after_cost_reward_risk",
            "after_cost_reward_risk",
            "reward_risk_ratio",
        ),
    }


def _trade_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = _numbers(rows, "net_return_pct")
    r_values = _numbers(rows, "r_multiple")
    pnl_values = _numbers(rows, "net_pnl")
    expected_hits = _numbers(rows, "expected_hit_rate_pct")
    expected_rs = _numbers(rows, "expected_r_multiple")
    wins = sum(1 for value in pnl_values if value > 0)
    losses = sum(1 for value in pnl_values if value < 0)
    observed_hit = round((wins / len(pnl_values)) * 100.0, 4) if pnl_values else None
    expected_hit = round(mean(expected_hits), 4) if expected_hits else None
    expected_r = round(mean(expected_rs), 4) if expected_rs else None
    observed_r = round(mean(r_values), 4) if r_values else None
    gross_values = _numbers(rows, "gross_pnl")
    return {
        "trade_count": len(rows),
        "wins": wins,
        "losses": losses,
        "flats": len(pnl_values) - wins - losses,
        "net_pnl": round(sum(pnl_values), 4) if pnl_values else None,
        "gross_pnl": round(sum(gross_values), 4) if gross_values else None,
        "average_net_return_pct": round(mean(returns), 4) if returns else None,
        "median_net_return_pct": round(median(returns), 4) if returns else None,
        "expected_hit_rate_pct": expected_hit,
        "observed_hit_rate_pct": observed_hit,
        "hit_rate_delta_pct": (
            round(observed_hit - expected_hit, 4)
            if observed_hit is not None and expected_hit is not None
            else None
        ),
        "expected_r_multiple": expected_r,
        "observed_r_multiple": observed_r,
        "r_delta": (
            round(observed_r - expected_r, 4)
            if observed_r is not None and expected_r is not None
            else None
        ),
        "missing_is_zero": False,
    }


def _bucket_summaries(
    rows: list[dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(field) or "unknown"), []).append(row)
    return [{"bucket": key, **_trade_summary(grouped[key])} for key in sorted(grouped)]


def _concentration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    gains = sorted(
        (value for value in _numbers(rows, "net_pnl") if value > 0),
        reverse=True,
    )
    losses = sorted(
        (abs(value) for value in _numbers(rows, "net_pnl") if value < 0),
        reverse=True,
    )
    return {
        "largest_gain_share_pct": (round((gains[0] / sum(gains)) * 100.0, 4) if gains else None),
        "largest_loss_share_pct": (round((losses[0] / sum(losses)) * 100.0, 4) if losses else None),
        "gain_count": len(gains),
        "loss_count": len(losses),
    }


def _symbol_concentration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl_by_symbol: dict[str, float] = {}
    count_by_symbol: Counter[str] = Counter()
    for row in rows:
        symbol = str(row.get("ticker") or "UNKNOWN").upper()
        pnl = _number(row.get("net_pnl"))
        if pnl is not None:
            pnl_by_symbol[symbol] = pnl_by_symbol.get(symbol, 0.0) + pnl
        count_by_symbol[symbol] += 1
    total_abs = sum(abs(value) for value in pnl_by_symbol.values())
    dominant = (
        max(pnl_by_symbol, key=lambda key: abs(pnl_by_symbol[key])) if pnl_by_symbol else None
    )
    most_frequent = count_by_symbol.most_common(1)[0] if count_by_symbol else None
    return {
        "dominant_pnl_symbol": dominant,
        "dominant_absolute_pnl_share_pct": (
            round((abs(pnl_by_symbol[dominant]) / total_abs) * 100.0, 4)
            if dominant is not None and total_abs > 0
            else None
        ),
        "most_frequent_symbol": most_frequent[0] if most_frequent else None,
        "most_frequent_trade_share_pct": (
            round((most_frequent[1] / len(rows)) * 100.0, 4) if most_frequent and rows else None
        ),
    }


def _gate_effectiveness(
    evaluations: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    trade_by_signal = {
        str(row.get("signal_id") or ""): row for row in trades if str(row.get("signal_id") or "")
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in evaluations:
        key = str(row.get("terminal_state") or row.get("reconciliation_status") or "unknown")
        grouped.setdefault(key, []).append(row)
    output = []
    for key in sorted(grouped):
        group = grouped[key]
        linked = [
            trade_by_signal[str(row.get("signal_id") or "")]
            for row in group
            if str(row.get("signal_id") or "") in trade_by_signal
        ]
        output.append(
            {
                "gate_outcome": key,
                "evaluation_count": len(group),
                "closed_trade_count": len(linked),
                "conversion_pct": round((len(linked) / len(group)) * 100.0, 4),
                "closed_trade_performance": _trade_summary(linked),
            }
        )
    actions = Counter(str(row.get("action") or "unknown") for row in intents)
    output.append(
        {
            "gate_outcome": "intent_actions",
            "evaluation_count": len(intents),
            "closed_trade_count": actions.get("ENTER_LONG", 0),
            "conversion_pct": (
                round((actions.get("ENTER_LONG", 0) / len(intents)) * 100.0, 4) if intents else None
            ),
            "action_counts": dict(sorted(actions.items())),
        }
    )
    return output


def _entry_failure_modes(
    evaluations: list[dict[str, Any]],
    intents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for row in evaluations:
        if row.get("filled") is not True:
            counts[str(row.get("terminal_state") or "evaluation_not_filled")] += 1
    for row in intents:
        if str(row.get("action") or "").upper() != "ENTER_LONG":
            counts[str(row.get("blocked_reason") or row.get("reason") or "intent_blocked")] += 1
    total = sum(counts.values())
    return [
        {
            "mode": key,
            "count": counts[key],
            "share_pct": round((counts[key] / total) * 100.0, 4) if total else None,
        }
        for key in sorted(counts, key=lambda item: (-counts[item], item))
    ]


def _exit_modes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("exit_reason") or "unknown"), []).append(row)
    return [{"exit_reason": key, **_trade_summary(grouped[key])} for key in sorted(grouped)]


def _numbers(rows: list[dict[str, Any]], field: str) -> list[float]:
    return [value for row in rows if (value := _number(row.get(field))) is not None]


def _first_number(row: dict[str, Any], *fields: str) -> float | None:
    for field in fields:
        value = _number(row.get(field))
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _gap_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 15:
        return "under_15"
    if value <= 30:
        return "15_to_30"
    if value <= 50:
        return "30_to_50"
    return "over_50"


def _float_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 5_000_000:
        return "under_5m"
    if value < 20_000_000:
        return "5m_to_20m"
    return "over_20m"


def _liquidity_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 1_000_000:
        return "under_1m"
    if value < 5_000_000:
        return "1m_to_5m"
    return "over_5m"


def _confidence_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 50:
        return "under_50"
    if value < 80:
        return "50_to_79"
    return "80_plus"


def _within(value: str, start: str | None, end: str | None) -> bool:
    if not value:
        return False
    return (start is None or value >= start[:10]) and (end is None or value <= end[:10])


def _row_date(row: dict[str, Any]) -> str:
    return str(row.get("market_date") or row.get("selected_at") or row.get("decision_time") or "")[
        :10
    ]


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str))


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _markdown(report: dict[str, Any]) -> str:
    official = dict(report.get("official") or {})
    coverage = dict(report.get("outcome_coverage") or {})
    concentration = dict(report.get("loss_concentration") or {})
    lines = [
        "# Dawnstrike AlphaOps causal attribution",
        "",
        f"- Generated: {report.get('generated_at')}",
        f"- Evidence cutoff: {report.get('evidence_cutoff') or 'not available'}",
        f"- Official closed trades: {official.get('trade_count')}",
        f"- Official net P&L: {_display(official.get('net_pnl'))}",
        f"- Observed hit rate: {_display(official.get('observed_hit_rate_pct'), '%')}",
        f"- Expected hit rate: {_display(official.get('expected_hit_rate_pct'), '%')}",
        f"- Observed average R: {_display(official.get('observed_r_multiple'))}",
        f"- Expected average R: {_display(official.get('expected_r_multiple'))}",
        f"- Outcome coverage: {_display(coverage.get('coverage_pct'), '%')}",
        f"- Largest loss concentration: "
        f"{_display(concentration.get('largest_loss_share_pct'), '%')}",
        "",
        "Missing values are not zero. This report is research-only and does not "
        "establish profitability or authorize promotion.",
    ]
    return "\n".join(lines) + "\n"


def _display(value: Any, suffix: str = "") -> str:
    parsed = _number(value)
    return f"{parsed:.4f}{suffix}" if parsed is not None else "not available"


__all__ = [
    "ATTRIBUTION_VERSION",
    "build_alpha_attribution_report",
    "generate_alpha_attribution_report",
]
