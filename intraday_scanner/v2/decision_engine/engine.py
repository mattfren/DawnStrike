"""Artifact-backed current decision engine for Titan Buildroom."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DecisionEngineResult:
    status: str
    output_root: Path
    decision_card_count: int
    watchlist_count: int
    blocked_count: int
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "blocked_count": self.blocked_count,
            "decision_card_count": self.decision_card_count,
            "output_root": self.output_root.as_posix(),
            "status": self.status,
            "warnings": list(self.warnings),
            "watchlist_count": self.watchlist_count,
        }


def build_decision_engine(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_titan"),
    alpha_root: Path = Path("data/v2_alpha_lab"),
    data_truth_root: Path = Path("data/v2_data_truth"),
    paper_ops_root: Path = Path("data/v2_paper_ops"),
) -> DecisionEngineResult:
    """Build current candidates, watchlist, blocked rows, and Markdown summary."""

    paths = _DecisionPaths.create(output_root / "decision_engine")
    scan_payload = _read_json(alpha_root / "scans" / "latest_scan.json", {})
    cards = _list(scan_payload.get("cards") if isinstance(scan_payload, dict) else [])
    no_setup = _list(scan_payload.get("no_setup") if isinstance(scan_payload, dict) else [])
    data_status = _data_truth_status(data_truth_root)
    data_manifest = _read_json(data_truth_root / "manifests" / "latest.json", {})
    evidence_by_strategy = _evidence_by_strategy(paper_ops_root)
    comparison_by_strategy = _comparison_by_strategy(alpha_root)
    paper_by_strategy = _paper_by_strategy(alpha_root)
    robustness_by_strategy = _robustness_by_strategy(alpha_root)
    readiness = _read_json(paper_ops_root / "reports" / "forward_readiness.json", {})
    readiness_status = (
        str(readiness.get("status", "unknown")) if isinstance(readiness, dict) else "unknown"
    )
    readiness_warnings = tuple(
        str(item)
        for item in _list_any(readiness.get("warnings") if isinstance(readiness, dict) else [])
    )
    accepted_end = _manifest_value(data_manifest, "accepted_end")
    global_warnings = _global_warnings(data_status, readiness_status, readiness_warnings)

    enriched_cards = [
        _enrich_card(
            card,
            run_date=run_date,
            data_status=data_status,
            data_manifest=data_manifest,
            accepted_end=accepted_end,
            evidence_by_strategy=evidence_by_strategy,
            comparison_by_strategy=comparison_by_strategy,
            paper_by_strategy=paper_by_strategy,
            robustness_by_strategy=robustness_by_strategy,
            global_warnings=global_warnings,
        )
        for card in cards
    ]
    blocked = [card for card in enriched_cards if card["status"] == "blocked"]
    current = [card for card in enriched_cards if card["status"] != "blocked"]
    watchlist = [
        _watchlist_row(
            row,
            run_date=run_date,
            data_status=data_status,
            accepted_end=accepted_end,
            evidence_by_strategy=evidence_by_strategy,
            comparison_by_strategy=comparison_by_strategy,
            robustness_by_strategy=robustness_by_strategy,
            global_warnings=global_warnings,
        )
        for row in no_setup[:50]
    ]
    near_setups = [
        row
        for row in watchlist
        if row["strategy_evidence_status"] not in {"quarantined", "rejected"}
        and bool(row["strategy_robustness_eligible"])
    ][:25]

    _write_json(paths.root / "decision_cards.json", enriched_cards)
    _write_json(paths.root / "current_candidates.json", current)
    _write_json(paths.root / "watchlist.json", watchlist)
    _write_json(paths.root / "near_setups.json", near_setups)
    _write_json(paths.root / "blocked_candidates.json", blocked)
    _write_csv(paths.root / "current_candidates.csv", current)
    _write_csv(paths.root / "watchlist.csv", watchlist)
    _write_markdown(
        paths.root / "decision_summary.md",
        run_date=run_date,
        current=current,
        watchlist=watchlist,
        near_setups=near_setups,
        blocked=blocked,
        data_status=data_status,
        warnings=global_warnings,
    )
    status = "passed_with_warnings" if global_warnings else "passed"
    result = DecisionEngineResult(
        status=status,
        output_root=paths.root,
        decision_card_count=len(enriched_cards),
        watchlist_count=len(watchlist),
        blocked_count=len(blocked),
        warnings=global_warnings,
    )
    _write_json(paths.root / "decision_engine_result.json", result.to_dict())
    return result


@dataclass(frozen=True)
class _DecisionPaths:
    root: Path

    @classmethod
    def create(cls, root: Path) -> _DecisionPaths:
        root.mkdir(parents=True, exist_ok=True)
        return cls(root=root)


def _enrich_card(
    card: dict[str, object],
    *,
    run_date: date,
    data_status: str,
    data_manifest: object,
    accepted_end: str,
    evidence_by_strategy: dict[str, dict[str, object]],
    comparison_by_strategy: dict[str, dict[str, object]],
    paper_by_strategy: dict[str, dict[str, object]],
    robustness_by_strategy: dict[str, dict[str, object]],
    global_warnings: tuple[str, ...],
) -> dict[str, object]:
    strategy_id = str(card.get("strategy_id", "unknown"))
    evidence = evidence_by_strategy.get(strategy_id, {})
    comparison = comparison_by_strategy.get(strategy_id, {})
    paper = paper_by_strategy.get(strategy_id, {})
    robustness = robustness_by_strategy.get(strategy_id, {})
    robustness_status = _robustness_status(strategy_id, robustness)
    robustness_eligible = _robustness_eligible(strategy_id, robustness)
    entry = _entry_price(card)
    quantity, notional, max_loss = _sizing(card, entry)
    warnings = (
        list(_list_any(card.get("warnings")))
        + list(global_warnings)
        + list(_robustness_warning_labels(strategy_id, robustness))
    )
    if _card_date_after_accepted_end(card, accepted_end):
        warnings.append("candidate_bar_after_datatruth_accepted_end")
    reasons = _reasons_to_avoid(card, evidence, data_status, warnings)
    status = "blocked" if _is_blocked(evidence, data_status, warnings) else str(
        card.get("status", "candidate")
    )
    return {
        **card,
        "run_date": run_date.isoformat(),
        "status": status,
        "data_truth_status": data_status,
        "data_truth_snapshot_id": _manifest_value(data_manifest, "snapshot_id"),
        "strategy_evidence_status": str(evidence.get("evidence_status", "unknown")),
        "strategy_evidence_score": evidence.get("overall_score", "n/a"),
        "sizing_quantity": quantity,
        "notional_exposure": round(notional, 4),
        "max_loss_estimate": round(max_loss, 4),
        "historical_backtest_summary": card.get("historical_summary", "n/a"),
        "replay_summary": _evidence_summary(evidence, "replay"),
        "forward_paper_summary": _evidence_summary(evidence, "forward"),
        "paper_pnl_summary": _paper_summary(paper),
        "backtest_rank": comparison.get("rank_by_return", "n/a"),
        "backtest_return_pct": comparison.get("total_return_pct", "n/a"),
        "strategy_robustness_status": robustness_status,
        "strategy_robustness_eligible": robustness_eligible,
        "strategy_robustness_warnings": _robustness_text(robustness),
        "robustness_test_return_pct": robustness.get("test_return_pct", "n/a"),
        "robustness_test_trade_count": robustness.get("test_trade_count", "n/a"),
        "robustness_cost_stress_delta_pct": robustness.get("cost_stress_delta_pct", "n/a"),
        "robustness_monte_carlo_worst_drawdown_pct": robustness.get(
            "monte_carlo_worst_drawdown_pct",
            "n/a",
        ),
        "warnings": list(dict.fromkeys(str(item) for item in warnings)),
        "reasons_to_avoid": reasons,
        "research_only": True,
    }


def _watchlist_row(
    row: dict[str, object],
    *,
    run_date: date,
    data_status: str,
    accepted_end: str,
    evidence_by_strategy: dict[str, dict[str, object]],
    comparison_by_strategy: dict[str, dict[str, object]],
    robustness_by_strategy: dict[str, dict[str, object]],
    global_warnings: tuple[str, ...],
) -> dict[str, object]:
    strategy_id = str(row.get("strategy_id", "unknown"))
    evidence = evidence_by_strategy.get(strategy_id, {})
    comparison = comparison_by_strategy.get(strategy_id, {})
    robustness = robustness_by_strategy.get(strategy_id, {})
    robustness_status = _robustness_status(strategy_id, robustness)
    robustness_eligible = _robustness_eligible(strategy_id, robustness)
    warnings = (
        list(_list_any(row.get("warnings")))
        + list(global_warnings)
        + list(_robustness_warning_labels(strategy_id, robustness))
    )
    if _card_date_after_accepted_end(row, accepted_end):
        warnings.append("candidate_bar_after_datatruth_accepted_end")
    return {
        "symbol": row.get("symbol", "n/a"),
        "run_date": run_date.isoformat(),
        "timestamp": row.get("timestamp", "n/a"),
        "strategy_id": strategy_id,
        "strategy_version": row.get("strategy_version", "n/a"),
        "status": "watchlist" if robustness_eligible else "blocked_watchlist",
        "setup_score": row.get("setup_score", 0),
        "data_truth_status": data_status,
        "strategy_evidence_status": evidence.get("evidence_status", "unknown"),
        "strategy_evidence_score": evidence.get("overall_score", "n/a"),
        "strategy_robustness_status": robustness_status,
        "strategy_robustness_eligible": robustness_eligible,
        "strategy_robustness_warnings": _robustness_text(robustness),
        "robustness_test_return_pct": robustness.get("test_return_pct", "n/a"),
        "robustness_test_trade_count": robustness.get("test_trade_count", "n/a"),
        "backtest_rank": comparison.get("rank_by_return", "n/a"),
        "entry_trigger": row.get("entry_trigger", "No current trigger."),
        "reason": "No current setup; watch for the mechanical trigger to appear.",
        "warnings": list(dict.fromkeys(str(item) for item in warnings)),
        "reasons_to_avoid": _reasons_to_avoid(row, evidence, data_status, warnings),
        "research_only": True,
    }


def _data_truth_status(root: Path) -> str:
    payload = _read_json(root / "reconciliation" / "latest_reconciliation.json", {})
    if isinstance(payload, dict):
        report = payload.get("report")
        if isinstance(report, dict):
            return str(report.get("status", "unknown"))
        return str(payload.get("status", "unknown"))
    return "unknown"


def _evidence_by_strategy(root: Path) -> dict[str, dict[str, object]]:
    payload = _read_json(root / "reports" / "strategy_evidence_scores.json", {})
    rows = _list(payload.get("scores") if isinstance(payload, dict) else [])
    return {str(row.get("strategy_id")): row for row in rows}


def _comparison_by_strategy(root: Path) -> dict[str, dict[str, object]]:
    payload = _read_json(root / "reports" / "strategy_comparison.json", [])
    rows = _list(payload)
    return {str(row.get("strategy_id")): row for row in rows}


def _paper_by_strategy(root: Path) -> dict[str, dict[str, object]]:
    payload = _read_json(root / "paper" / "strategy_pnl.json", [])
    rows = _list(payload)
    return {str(row.get("strategy_id")): row for row in rows}


def _robustness_by_strategy(root: Path) -> dict[str, dict[str, object]]:
    payload = _read_json(root / "reports" / "robustness_summary.json", {})
    rows = _list(payload.get("rows") if isinstance(payload, dict) else [])
    return {str(row.get("strategy_id")): row for row in rows}


def _global_warnings(
    data_status: str,
    readiness_status: str,
    readiness_warnings: tuple[str, ...],
) -> tuple[str, ...]:
    warnings = list(readiness_warnings)
    if readiness_status == "blocked":
        warnings.append("paper_ops_readiness_blocked")
    if data_status == "single_provider_unreconciled":
        warnings.append("single_provider_data_not_reconciled")
    if data_status in {"mismatch", "provider_disagreement", "insufficient_overlap"}:
        warnings.append(f"data_truth_status_{data_status}")
    return tuple(dict.fromkeys(warnings))


def _entry_price(card: dict[str, object]) -> float | None:
    trigger = str(card.get("entry_trigger", ""))
    match = re.search(r"close\s+([0-9]+(?:\.[0-9]+)?)", trigger)
    if match:
        return float(match.group(1))
    stop = _to_float(card.get("stop"))
    risk = _to_float(card.get("risk_per_share"))
    direction = str(card.get("direction", "long"))
    if stop is not None and risk is not None:
        return stop + risk if direction == "long" else stop - risk
    return None


def _sizing(card: dict[str, object], entry: float | None) -> tuple[int, float, float]:
    risk_per_share = _to_float(card.get("risk_per_share"))
    if entry is None or risk_per_share is None or entry <= 0 or risk_per_share <= 0:
        return 0, 0.0, 0.0
    account_equity = 100_000.0
    quantity_by_risk = int((account_equity * 0.01) // risk_per_share)
    quantity_by_notional = int((account_equity * 0.20) // entry)
    quantity = max(0, min(quantity_by_risk, quantity_by_notional))
    return quantity, quantity * entry, quantity * risk_per_share


def _reasons_to_avoid(
    card: dict[str, object],
    evidence: dict[str, object],
    data_status: str,
    warnings: list[object],
) -> list[str]:
    reasons: list[str] = []
    if data_status == "single_provider_unreconciled":
        reasons.append("Data is single-provider and not independently reconciled.")
    if "paper_ops_readiness_blocked" in {str(item) for item in warnings}:
        reasons.append("PaperOps readiness is blocked.")
    if "candidate_bar_after_datatruth_accepted_end" in {str(item) for item in warnings}:
        reasons.append("Candidate is newer than the latest completed DataTruth bar.")
    if evidence.get("evidence_status") in {"quarantined", "rejected"}:
        reasons.append(f"Strategy evidence status is {evidence.get('evidence_status')}.")
    warning_values = {str(item) for item in warnings}
    if "strategy_robustness_fragile" in warning_values:
        reasons.append("Alpha Lab robustness flags this strategy as fragile.")
    if "strategy_robustness_insufficient_oos_trades" in warning_values:
        reasons.append("Alpha Lab robustness has insufficient out-of-sample trades.")
    if evidence.get("blockers"):
        reasons.append(str(evidence["blockers"]))
    if card.get("reward_risk") in {None, "", "n/a"}:
        reasons.append("No reward/risk value is available.")
    if warnings:
        reasons.append("Warnings require review before any paper decision.")
    return reasons or ["No hard avoid reason beyond research-only status."]


def _is_blocked(
    evidence: dict[str, object],
    data_status: str,
    warnings: list[object],
) -> bool:
    if data_status in {"mismatch", "provider_disagreement", "insufficient_overlap"}:
        return True
    if data_status == "single_provider_unreconciled":
        return True
    if "paper_ops_readiness_blocked" in {str(item) for item in warnings}:
        return True
    if "candidate_bar_after_datatruth_accepted_end" in {str(item) for item in warnings}:
        return True
    if "strategy_robustness_fragile" in {str(item) for item in warnings}:
        return True
    if "strategy_robustness_insufficient_oos_trades" in {str(item) for item in warnings}:
        return True
    if evidence.get("evidence_status") in {"quarantined", "rejected"}:
        return True
    return "invalid_stop_or_entry" in {str(item) for item in warnings}


def _card_date_after_accepted_end(card: dict[str, object], accepted_end: str) -> bool:
    timestamp = str(card.get("timestamp", ""))
    if not timestamp or not accepted_end or accepted_end == "unknown":
        return False
    return timestamp[:10] > accepted_end


def _evidence_summary(evidence: dict[str, object], scope: str) -> str:
    days = evidence.get(f"{scope}_days")
    trades = evidence.get(f"{scope}_closed_trades")
    expectancy = evidence.get(f"{scope}_expectancy", evidence.get("expectancy"))
    return f"{days or 0} days, {trades or 0} closed trades, expectancy {expectancy or 0}."


def _paper_summary(row: dict[str, object]) -> str:
    if not row:
        return "No paper P&L row available."
    return (
        f"{row.get('trade_count', 0)} trades, net P&L {row.get('net_pnl', 0)}, "
        f"return {row.get('return_on_equity', 0)}."
    )


def _robustness_status(strategy_id: str, row: dict[str, object]) -> str:
    if not row:
        return "unknown"
    if _is_benchmark_or_baseline(strategy_id, row):
        return str(row.get("robustness_status", "non_candidate"))
    return str(row.get("robustness_status", "unknown"))


def _robustness_eligible(strategy_id: str, row: dict[str, object]) -> bool:
    if not row:
        return True
    if _is_benchmark_or_baseline(strategy_id, row):
        return True
    return str(row.get("robustness_status", "unknown")) not in {
        "fragile",
        "insufficient_oos_trades",
    }


def _robustness_warning_labels(strategy_id: str, row: dict[str, object]) -> tuple[str, ...]:
    if _robustness_eligible(strategy_id, row):
        return ()
    status = str(row.get("robustness_status", "unknown"))
    if status == "fragile":
        return ("strategy_robustness_fragile",)
    if status == "insufficient_oos_trades":
        return ("strategy_robustness_insufficient_oos_trades",)
    return (f"strategy_robustness_{status}",)


def _robustness_text(row: dict[str, object]) -> str:
    if not row:
        return "No robustness row available."
    return str(row.get("warnings", "none"))


def _is_benchmark_or_baseline(strategy_id: str, row: dict[str, object]) -> bool:
    status = str(row.get("status", "")).lower()
    return (
        status in {"benchmark", "baseline"}
        or strategy_id.startswith("benchmark_")
        or strategy_id.startswith("cash_")
    )


def _manifest_value(payload: object, key: str) -> str:
    return str(payload.get(key, "unknown")) if isinstance(payload, dict) else "unknown"


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = tuple(sorted({key for row in rows for key in row}))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _write_markdown(
    path: Path,
    *,
    run_date: date,
    current: list[dict[str, object]],
    watchlist: list[dict[str, object]],
    near_setups: list[dict[str, object]],
    blocked: list[dict[str, object]],
    data_status: str,
    warnings: tuple[str, ...],
) -> None:
    lines = [
        "# Titan Decision Engine",
        "",
        f"- Run date: `{run_date.isoformat()}`",
        f"- DataTruth status: `{data_status}`",
        f"- Current candidates: `{len(current)}`",
        f"- Watchlist rows: `{len(watchlist)}`",
        f"- Near setups: `{len(near_setups)}`",
        f"- Blocked candidates: `{len(blocked)}`",
        "- Boundary: research-only; no broker routing or live execution.",
        "",
        "## Current Candidates",
        "",
    ]
    if current:
        for row in current[:20]:
            lines.append(
                f"- `{row['symbol']}` `{row['strategy_id']}` score "
                f"`{row['setup_score']}`, R:R `{row.get('reward_risk', 'n/a')}`, "
                f"robustness `{row.get('strategy_robustness_status', 'unknown')}`, "
                f"size `{row['sizing_quantity']}`, max loss "
                f"`{row['max_loss_estimate']}`."
            )
    else:
        lines.append("- None.")
    lines.extend(["", "## Blocked Candidates", ""])
    if blocked:
        for row in blocked[:20]:
            reasons = " | ".join(
                str(item) for item in _list_any(row.get("reasons_to_avoid"))
            )
            lines.append(
                f"- `{row['symbol']}` `{row['strategy_id']}` blocked; "
                f"robustness `{row.get('strategy_robustness_status', 'unknown')}`; "
                f"reasons: {reasons}"
            )
    else:
        lines.append("- None.")
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {warning}" for warning in warnings or ("None.",))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _list_any(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _to_float(value: object) -> float | None:
    if value in {None, ""}:
        return None
    if isinstance(value, int | float | str):
        return float(value)
    return None


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " | ".join(str(item) for item in value)
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)
