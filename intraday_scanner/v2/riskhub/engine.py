"""Portfolio-level research risk report for Titan Buildroom."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RiskHubResult:
    status: str
    output_root: Path
    kill_switch: bool
    warnings: tuple[str, ...]
    total_candidate_risk: float
    total_candidate_notional: float

    def to_dict(self) -> dict[str, object]:
        return {
            "kill_switch": self.kill_switch,
            "output_root": self.output_root.as_posix(),
            "status": self.status,
            "total_candidate_notional": round(self.total_candidate_notional, 4),
            "total_candidate_risk": round(self.total_candidate_risk, 4),
            "warnings": list(self.warnings),
        }


def build_risk_report(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_titan"),
    paper_ops_root: Path = Path("data/v2_paper_ops"),
) -> RiskHubResult:
    paths = _RiskPaths.create(output_root / "risk")
    decision_cards = _list(_read_json(output_root / "decision_engine" / "decision_cards.json", []))
    pending_orders = _list(_read_json(paper_ops_root / "state" / "pending_orders.json", []))
    open_positions = _list(_read_json(paper_ops_root / "state" / "open_positions.json", []))
    readiness = _read_json(paper_ops_root / "reports" / "forward_readiness.json", {})
    evidence = _strategy_evidence(paper_ops_root)
    commitbridge = _commitbridge_status()

    candidate_risk = sum(_to_float(card.get("max_loss_estimate")) for card in decision_cards)
    candidate_notional = sum(_to_float(card.get("notional_exposure")) for card in decision_cards)
    strategy_exposure = _aggregate(decision_cards, "strategy_id", "notional_exposure")
    symbol_exposure = _aggregate(decision_cards, "symbol", "notional_exposure")
    warnings = _warnings(
        decision_cards=decision_cards,
        pending_orders=pending_orders,
        open_positions=open_positions,
        readiness=readiness,
        evidence=evidence,
        commitbridge=commitbridge,
        candidate_risk=candidate_risk,
        candidate_notional=candidate_notional,
    )
    kill_switch = any(
        warning
        in {
            "paper_ops_readiness_blocked",
            "ledger_or_calendar_truth_failed",
            "candidate_has_invalid_risk",
            "candidate_uses_quarantined_strategy",
            "candidate_blocked_by_decision_engine",
            "candidate_uses_fragile_strategy",
            "candidate_lacks_oos_robustness",
        }
        for warning in warnings
    )
    status = "blocked" if kill_switch else ("passed_with_warnings" if warnings else "passed")
    result = RiskHubResult(
        status=status,
        output_root=paths.root,
        kill_switch=kill_switch,
        warnings=warnings,
        total_candidate_risk=candidate_risk,
        total_candidate_notional=candidate_notional,
    )
    payload = {
        **result.to_dict(),
        "run_date": run_date.isoformat(),
        "account_equity_assumption": 100_000.0,
        "candidate_count": len(decision_cards),
        "pending_orders": len(pending_orders),
        "open_positions": len(open_positions),
        "commitbridge": commitbridge,
        "commit_policy": {
            "commit_allowed": commitbridge.get("proposals_eligible", 0),
            "commit_blocked": commitbridge.get("proposals_blocked", 0),
            "commit_requires_manual_review": commitbridge.get("uncommitted_overlay_count", 0),
            "pending_order_resolution_allowed": commitbridge.get("proposals_eligible", 0),
            "pending_order_resolution_blocked": commitbridge.get("proposals_blocked", 0),
            "research_only": True,
        },
        "strategy_exposure": strategy_exposure,
        "symbol_exposure": symbol_exposure,
        "policy": {
            "max_candidate_risk_pct": 0.03,
            "max_candidate_notional_pct": 0.60,
            "max_symbol_exposure_pct": 0.30,
            "max_strategy_exposure_pct": 0.40,
            "research_only": True,
            "live_execution_allowed": False,
            "fragile_strategy_action": "kill_switch",
            "insufficient_oos_trades_action": "kill_switch",
        },
    }
    _write_json(paths.root / "risk_report.json", payload)
    _write_csv(paths.root / "strategy_exposure.csv", strategy_exposure)
    _write_csv(paths.root / "symbol_exposure.csv", symbol_exposure)
    _write_markdown(paths.root / "risk_report.md", payload)
    return result


@dataclass(frozen=True)
class _RiskPaths:
    root: Path

    @classmethod
    def create(cls, root: Path) -> _RiskPaths:
        root.mkdir(parents=True, exist_ok=True)
        return cls(root=root)


def _warnings(
    *,
    decision_cards: list[dict[str, object]],
    pending_orders: list[dict[str, object]],
    open_positions: list[dict[str, object]],
    readiness: object,
    evidence: dict[str, dict[str, object]],
    commitbridge: dict[str, object],
    candidate_risk: float,
    candidate_notional: float,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if isinstance(readiness, dict) and readiness.get("status") == "blocked":
        warnings.append("paper_ops_readiness_blocked")
    if isinstance(readiness, dict) and (
        readiness.get("ledger_rebuild_status") != "passed"
        or readiness.get("calendar_truth_status") != "passed"
    ):
        warnings.append("ledger_or_calendar_truth_failed")
    if commitbridge.get("status") in {"failed", "resume_required"}:
        warnings.append("commitbridge_not_verified")
    if _to_float(commitbridge.get("proposals_blocked")) > 0:
        warnings.append("commitbridge_blocks_unsafe_filltruth_commit")
    if _to_float(commitbridge.get("uncommitted_overlay_count")) > 0:
        warnings.append("commitbridge_uncommitted_overlay_requires_review")
    if commitbridge.get("pending_divergence_status") == "unresolved_uncommitted_eligible_overlay":
        warnings.append("commitbridge_pending_divergence_unresolved")
    if candidate_risk > 3_000:
        warnings.append("candidate_risk_exceeds_3pct_equity")
    if candidate_notional > 60_000:
        warnings.append("candidate_notional_exceeds_60pct_equity")
    if len(open_positions) >= 3:
        warnings.append("max_open_positions_reached_or_near")
    if len(pending_orders) >= 3:
        warnings.append("pending_order_queue_needs_review")
    for card in decision_cards:
        if _to_float(card.get("max_loss_estimate")) <= 0 or _to_float(card.get("reward_risk")) <= 0:
            warnings.append("candidate_has_invalid_risk")
        if card.get("status") == "blocked":
            warnings.append("candidate_blocked_by_decision_engine")
        strategy_id = str(card.get("strategy_id", "unknown"))
        strategy_evidence = evidence.get(strategy_id, {})
        if strategy_evidence.get("evidence_status") == "quarantined":
            warnings.append("candidate_uses_quarantined_strategy")
        robustness_status = str(card.get("strategy_robustness_status", "unknown"))
        if robustness_status == "fragile":
            warnings.append("candidate_uses_fragile_strategy")
        if robustness_status == "insufficient_oos_trades":
            warnings.append("candidate_lacks_oos_robustness")
        if card.get("data_truth_status") == "single_provider_unreconciled":
            warnings.append("candidate_data_single_provider")
    return tuple(dict.fromkeys(warnings))


def _strategy_evidence(root: Path) -> dict[str, dict[str, object]]:
    payload = _read_json(root / "reports" / "strategy_evidence_scores.json", {})
    rows = _list(payload.get("scores") if isinstance(payload, dict) else [])
    return {str(row.get("strategy_id")): row for row in rows}


def _commitbridge_status(root: Path = Path("data/v2_evidence_commit")) -> dict[str, object]:
    reconciliation = _read_json(root / "reconciliation" / "pending_divergence_latest.json", {})
    summary = _read_json(root / "reports" / "evidence_commit_summary.json", {})
    if not isinstance(reconciliation, dict):
        reconciliation = {}
    if not isinstance(summary, dict):
        summary = {}
    status = str(summary.get("status") or reconciliation.get("status") or "missing")
    return {
        "pending_after_commit": reconciliation.get("pending_after_commit", 0),
        "pending_before_commit": reconciliation.get("pending_before_commit", 0),
        "pending_divergence_status": reconciliation.get("pending_divergence_status", "missing"),
        "proposals_blocked": reconciliation.get("proposals_blocked", summary.get("blocked", 0)),
        "proposals_committed": reconciliation.get(
            "proposals_committed",
            summary.get("commit_events", 0),
        ),
        "proposals_created": reconciliation.get("proposals_created", summary.get("proposed", 0)),
        "proposals_eligible": reconciliation.get("proposals_eligible", summary.get("eligible", 0)),
        "proposals_rejected": reconciliation.get("proposals_rejected", summary.get("rejected", 0)),
        "status": status,
        "uncommitted_overlay_count": reconciliation.get("uncommitted_overlay_count", 0),
    }


def _aggregate(
    rows: list[dict[str, object]],
    key_field: str,
    value_field: str,
) -> list[dict[str, object]]:
    totals: dict[str, float] = {}
    for row in rows:
        key = str(row.get(key_field, "unknown"))
        totals[key] = totals.get(key, 0.0) + _to_float(row.get(value_field))
    return [
        {
            key_field: key,
            value_field: round(value, 4),
            "pct_equity": round(value / 100_000.0, 6),
        }
        for key, value in sorted(totals.items())
    ]


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
    fields = tuple(sorted({key for row in rows for key in row})) or ("empty",)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _write_markdown(path: Path, payload: dict[str, object]) -> None:
    lines = [
        "# Titan RiskHub",
        "",
        f"- Status: `{payload['status']}`",
        f"- Kill switch: `{payload['kill_switch']}`",
        f"- Candidate count: `{payload['candidate_count']}`",
        f"- Total candidate risk: `{payload['total_candidate_risk']}`",
        f"- Total candidate notional: `{payload['total_candidate_notional']}`",
        "- Boundary: research-only; no order routing.",
        "",
        "## Warnings",
        "",
    ]
    warnings = payload.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _to_float(value: object) -> float:
    if value in {None, ""}:
        return 0.0
    if isinstance(value, int | float | str):
        return float(value)
    return 0.0


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)
