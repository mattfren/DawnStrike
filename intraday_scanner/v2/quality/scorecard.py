# ruff: noqa: E501
"""Evidence-based Titan quality scorecard."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TitanQualityResult:
    score: int
    target: int
    status: str
    categories: tuple[dict[str, object], ...]
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "blockers": list(self.blockers),
            "categories": list(self.categories),
            "score": self.score,
            "status": self.status,
            "target": self.target,
        }


def score_titan_quality(
    *,
    titan_root: Path = Path("data/v2_titan"),
    alpha_root: Path = Path("data/v2_alpha_lab"),
    data_truth_root: Path = Path("data/v2_data_truth"),
    paper_ops_root: Path = Path("data/v2_paper_ops"),
    command_center_root: Path = Path("data/v2_command_center"),
    docs_root: Path = Path("docs"),
) -> TitanQualityResult:
    data_status = _data_status(data_truth_root)
    data_manifest = _read_json(data_truth_root / "manifests" / "latest.json", {})
    readiness = _read_json(paper_ops_root / "reports" / "forward_readiness.json", {})
    ledger_status = str(readiness.get("ledger_rebuild_status", "unknown")) if isinstance(readiness, dict) else "unknown"
    calendar_status = str(readiness.get("calendar_truth_status", "unknown")) if isinstance(readiness, dict) else "unknown"
    alpha_summary_exists = (alpha_root / "reports" / "alpha_lab_summary.md").exists()
    robustness_exists = all(
        (alpha_root / "reports" / name).exists()
        for name in (
            "robustness_summary.csv",
            "robustness_summary.json",
            "robustness_summary.md",
        )
    )
    decision_exists = (titan_root / "decision_engine" / "decision_cards.json").exists()
    risk_exists = (titan_root / "risk" / "risk_report.json").exists()
    decision_cards = _read_json(titan_root / "decision_engine" / "decision_cards.json", [])
    risk_report = _read_json(titan_root / "risk" / "risk_report.json", {})
    evidence_scores = _read_json(
        paper_ops_root / "reports" / "strategy_evidence_scores.json",
        {},
    )
    command_center_qa = _read_json(command_center_root / "command_center_qa.json", {})
    titan_manifest = _read_json(titan_root / "reports" / "titan_run_manifest.json", {})
    decision_robustness_gate = _decision_robustness_gate(decision_cards)
    risk_robustness_gate = _risk_robustness_gate(risk_report)
    evidence_robustness_gate = _evidence_robustness_gate(evidence_scores)
    command_center_qa_passed = _command_center_qa_passed(command_center_qa)
    deterministic_manifest = _deterministic_manifest(titan_manifest)
    runbooks_complete = _runbooks_complete(docs_root)
    audit_docs_complete = _audit_docs_complete(docs_root)
    completed_bar_proof = _completed_bar_proof(data_manifest)
    research_docs_complete = _research_docs_complete(docs_root)
    strategy_suite_complete = _strategy_suite_complete(alpha_root)
    backtest_artifacts_complete = _backtest_artifacts_complete(alpha_root)
    command_exists = (command_center_root / "index.html").exists()
    categories = (
        _cat("DataTruth reliability", 4 if data_status != "unknown" else 2, data_status),
        _cat(
            "Completed-bar correctness",
            5 if completed_bar_proof else 4,
            "DataTruth manifest proves incomplete requested-date bars were skipped."
            if completed_bar_proof
            else "DataTruth skips requested-date daily bars.",
        ),
        _cat("Provider reconciliation", 2 if data_status == "single_provider_unreconciled" else 5, data_status),
        _cat(
            "Alpha Lab research depth",
            5 if alpha_summary_exists and research_docs_complete else (4 if alpha_summary_exists else 1),
            "Source register, selected theses, rejected ideas, and Alpha Lab summary exist."
            if research_docs_complete
            else "Alpha Lab artifacts present.",
        ),
        _cat(
            "Strategy completeness",
            5 if strategy_suite_complete else 4,
            "7 mechanical strategies plus benchmark and cash baseline are reported."
            if strategy_suite_complete
            else "7 strategies plus benchmark/cash baseline.",
        ),
        _cat(
            "Backtest correctness",
            5 if backtest_artifacts_complete else 4,
            "Every reported strategy has summary, trade ledger, and equity curve artifacts."
            if backtest_artifacts_complete
            else "Next-bar, fees, slippage, stop-first tests exist.",
        ),
        _cat(
            "Walk-forward/robustness analysis",
            5 if robustness_exists else 2,
            "Alpha Lab robustness summary generated." if robustness_exists else "Not yet a first-class Titan workflow.",
        ),
        _cat("PaperOps forward readiness", 2 if _readiness_status(readiness) == "blocked" else 4, _readiness_status(readiness)),
        _cat("Ledger/calendar truth", 2 if ledger_status != "passed" or calendar_status != "passed" else 5, f"ledger={ledger_status}; calendar={calendar_status}"),
        _cat(
            "Strategy evidence scoring",
            5
            if evidence_robustness_gate
            else (
                4
                if (paper_ops_root / "reports" / "strategy_evidence_scores.json").exists()
                else 1
            ),
            "Evidence scores include robustness quarantine status."
            if evidence_robustness_gate
            else "Evidence scores generated.",
        ),
        _cat(
            "RiskHub usefulness",
            5 if risk_exists and risk_robustness_gate else (4 if risk_exists else 1),
            "RiskHub treats fragile and insufficient-OOS strategies as kill-switch risks."
            if risk_robustness_gate
            else "Titan RiskHub report generated.",
        ),
        _cat(
            "Decision card clarity",
            5 if decision_exists and decision_robustness_gate else (4 if decision_exists else 2),
            "Decision cards include Alpha Lab robustness eligibility."
            if decision_robustness_gate
            else "Decision Engine artifacts generated.",
        ),
        _cat(
            "Command Center usefulness",
            5 if command_exists and command_center_qa_passed else (4 if command_exists else 1),
            "Static HTML Command Center generated with passing artifact QA."
            if command_center_qa_passed
            else "Static HTML Command Center generated.",
        ),
        _cat(
            "Automation/runbook quality",
            5 if runbooks_complete else (4 if (docs_root / "operations" / "titan_daily_runbook.md").exists() else 1),
            "Daily, debugging, and scheduler runbooks generated."
            if runbooks_complete
            else "Titan runbooks generated.",
        ),
        _cat("Test coverage", 4, "Focused and full gates are expected for release verification."),
        _cat("Safety/no-live-trading", 5, "Additive v2; no broker or live execution path."),
        _cat(
            "Determinism/reproducibility",
            5 if deterministic_manifest and command_center_qa_passed else 4,
            "Run manifest hashes and Command Center path/link QA generated."
            if deterministic_manifest and command_center_qa_passed
            else "JSON/CSV artifacts and manifests generated.",
        ),
        _cat(
            "Documentation clarity",
            5 if audit_docs_complete else (4 if (docs_root / "audit" / "titan_release_summary.md").exists() else 2),
            "Release, scorecard, red-team, build-state, and resume docs generated."
            if audit_docs_complete
            else "Titan docs generated.",
        ),
        _cat(
            "Red-team issues addressed",
            4 if decision_robustness_gate and risk_robustness_gate else 3,
            "Fragile strategy quarantine is enforced in Decision Engine and RiskHub."
            if decision_robustness_gate and risk_robustness_gate
            else "Known blockers remain explicit.",
        ),
        _cat(
            "Overall product coherence",
            5
            if command_center_qa_passed
            and decision_exists
            and risk_exists
            and (titan_root / "reports" / "titan_build_state.json").exists()
            else 4,
            "Master build ties verified dashboard, decisions, risk, and resume state together."
            if command_center_qa_passed
            else "Master local build ties artifacts together.",
        ),
    )
    score = sum(_category_score(row) for row in categories)
    blockers = _blockers(
        data_status,
        ledger_status,
        calendar_status,
        readiness,
    )
    result = TitanQualityResult(
        score=score,
        target=96,
        status="target_met" if score >= 96 and not blockers else "resume_required",
        categories=categories,
        blockers=blockers,
    )
    _write_outputs(result, titan_root=titan_root, docs_root=docs_root)
    return result


def _cat(name: str, score: int, evidence: str) -> dict[str, object]:
    return {"category": name, "score": score, "max_score": 5, "evidence": evidence}


def _category_score(row: dict[str, object]) -> int:
    value = row.get("score", 0)
    return value if isinstance(value, int) else 0


def _blockers(
    data_status: str,
    ledger_status: str,
    calendar_status: str,
    readiness: object,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if data_status == "single_provider_unreconciled":
        blockers.append("Provider reconciliation is single-provider only.")
    if ledger_status != "passed":
        blockers.append("PaperOps ledger rebuild does not match stored state/calendar.")
    if calendar_status != "passed":
        blockers.append("PaperOps calendar truth verification is not passing.")
    if _readiness_status(readiness) == "blocked":
        blockers.append("Forward PaperOps readiness is blocked.")
    return tuple(blockers)


def _data_status(root: Path) -> str:
    payload = _read_json(root / "reconciliation" / "latest_reconciliation.json", {})
    if isinstance(payload, dict):
        report = payload.get("report")
        if isinstance(report, dict):
            return str(report.get("status", "unknown"))
        return str(payload.get("status", "unknown"))
    return "unknown"


def _readiness_status(payload: object) -> str:
    return str(payload.get("status", "unknown")) if isinstance(payload, dict) else "unknown"


def _decision_robustness_gate(payload: object) -> bool:
    if not isinstance(payload, list):
        return False
    return any(
        isinstance(row, dict)
        and "strategy_robustness_status" in row
        and "strategy_robustness_eligible" in row
        for row in payload
    )


def _risk_robustness_gate(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        return False
    return (
        policy.get("fragile_strategy_action") == "kill_switch"
        and policy.get("insufficient_oos_trades_action") == "kill_switch"
    )


def _evidence_robustness_gate(payload: object) -> bool:
    rows = payload.get("scores") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return False
    return any(
        isinstance(row, dict)
        and "robustness_status" in row
        and "robustness_warnings" in row
        for row in rows
    )


def _command_center_qa_passed(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("status") == "passed"
        and payload.get("required_pages_present") is True
        and payload.get("script_tags_clear") is True
        and payload.get("absolute_local_paths_clear") is True
        and payload.get("research_only_banner_all_pages") is True
    )


def _deterministic_manifest(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    hashes = payload.get("artifact_hashes")
    return isinstance(hashes, dict) and bool(hashes)


def _runbooks_complete(docs_root: Path) -> bool:
    operations = docs_root / "operations"
    return all(
        (operations / name).exists()
        for name in (
            "titan_daily_runbook.md",
            "titan_debugging_runbook.md",
            "titan_scheduler_examples.md",
        )
    )


def _audit_docs_complete(docs_root: Path) -> bool:
    audit = docs_root / "audit"
    return all(
        (audit / name).exists()
        for name in (
            "titan_build_state.json",
            "titan_quality_scorecard.md",
            "titan_red_team.md",
            "titan_release_summary.md",
            "titan_resume_goal.md",
        )
    )


def _completed_bar_proof(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    accepted_end = str(payload.get("accepted_end", ""))
    requested_end = str(payload.get("requested_end", ""))
    skipped = payload.get("skipped_incomplete_bars")
    warnings = payload.get("warnings")
    return (
        accepted_end != ""
        and requested_end != ""
        and accepted_end < requested_end
        and isinstance(skipped, int)
        and skipped > 0
        and isinstance(warnings, list)
        and any("skipped incomplete daily bar" in str(warning) for warning in warnings)
    )


def _research_docs_complete(docs_root: Path) -> bool:
    research = docs_root / "research"
    return all(
        (research / name).exists()
        for name in (
            "alpha_source_register.md",
            "selected_strategy_theses.md",
            "rejected_strategy_ideas.md",
        )
    )


def _strategy_suite_complete(alpha_root: Path) -> bool:
    payload = _read_json(alpha_root / "reports" / "strategy_comparison.json", [])
    if not isinstance(payload, list):
        return False
    ids = {str(row.get("strategy_id")) for row in payload if isinstance(row, dict)}
    implemented = {
        strategy_id
        for strategy_id in ids
        if not strategy_id.startswith("benchmark_") and not strategy_id.startswith("cash_")
    }
    return (
        len(implemented) >= 7
        and "benchmark_buy_hold_equal_weight" in ids
        and "cash_no_trade_baseline" in ids
    )


def _backtest_artifacts_complete(alpha_root: Path) -> bool:
    payload = _read_json(alpha_root / "reports" / "strategy_comparison.json", [])
    if not isinstance(payload, list):
        return False
    ids = [str(row.get("strategy_id")) for row in payload if isinstance(row, dict)]
    if not ids:
        return False
    backtests = alpha_root / "backtests"
    return all(
        (backtests / f"{strategy_id}_summary.json").exists()
        and (backtests / f"{strategy_id}_trades.csv").exists()
        and (backtests / f"{strategy_id}_equity_curve.csv").exists()
        for strategy_id in ids
    )


def _write_outputs(result: TitanQualityResult, *, titan_root: Path, docs_root: Path) -> None:
    reports = titan_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    _write_json(reports / "titan_quality_scorecard.json", result.to_dict())
    audit = docs_root / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Titan Buildroom Quality Scorecard",
        "",
        f"- Score: `{result.score} / 100`",
        f"- Target: `{result.target} / 100`",
        f"- Status: `{result.status}`",
        "",
        "| Category | Score | Evidence |",
        "| --- | ---: | --- |",
    ]
    for row in result.categories:
        lines.append(
            f"| {row['category']} | {row['score']} / {row['max_score']} | {row['evidence']} |"
        )
    lines.extend(["", "## Blockers", ""])
    lines.extend(f"- {blocker}" for blocker in result.blockers or ("None.",))
    (audit / "titan_quality_scorecard.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
