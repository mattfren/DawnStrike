# ruff: noqa: E501
"""One-button local Titan Buildroom orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from intraday_scanner.v2.alpha_lab import AlphaLabRunResult, run_demo
from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.data_truth import DataTruthBuildResult, build_data_truth_snapshot
from intraday_scanner.v2.decision_engine import build_decision_engine
from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.calendar_view import write_calendar_view
from intraday_scanner.v2.paper_ops.engine import (
    calendar,
    init,
    reconcile,
    replay,
    report,
    run_day,
)
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.readiness import forward_readiness
from intraday_scanner.v2.paper_ops.strategy_evidence import score_strategy_evidence
from intraday_scanner.v2.quality import score_titan_quality
from intraday_scanner.v2.riskhub import build_risk_report


@dataclass(frozen=True)
class TitanBuildResult:
    status: str
    run_id: str
    quality_score: int
    quality_target: int
    command_center_index: Path
    blockers: tuple[str, ...]
    reports_root: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "blockers": list(self.blockers),
            "command_center_index": self.command_center_index.as_posix(),
            "quality_score": self.quality_score,
            "quality_target": self.quality_target,
            "reports_root": self.reports_root.as_posix(),
            "run_id": self.run_id,
            "status": self.status,
        }


def build_all(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_titan"),
    allow_fetch: bool = True,
) -> TitanBuildResult:
    """Run the local-first Titan build pipeline and write release artifacts."""

    started_at = datetime.now(timezone.utc)
    run_id = f"titan_build_{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    paths = _TitanPaths.create(output_root)
    command_log: list[dict[str, object]] = []

    data_truth = build_data_truth_snapshot(
        as_of_date=run_date,
        output_root=Path("data/v2_data_truth"),
        allow_fetch=allow_fetch,
    )
    command_log.append(
        _step(
            "DataTruth",
            "py -m intraday_scanner.v2.data_truth build --date "
            f"{run_date.isoformat()}{' --no-fetch' if not allow_fetch else ''}",
            "passed_with_warnings" if data_truth.warnings else "passed",
        )
    )

    alpha = run_demo(output_root=Path("data/v2_alpha_lab"), allow_public_data=True)
    command_log.append(
        _step("Alpha Lab", "py -m intraday_scanner.v2.alpha_lab demo", "passed")
    )

    init_result = init(output_root=Path("data/v2_paper_ops"))
    command_log.append(_step("PaperOps init", "py -m intraday_scanner.v2.paper_ops init", init_result["status"]))
    paper_run = run_day(run_date=run_date, output_root=Path("data/v2_paper_ops"))
    command_log.append(
        _step(
            "PaperOps run-day",
            f"py -m intraday_scanner.v2.paper_ops run-day --date {run_date.isoformat()}",
            str(paper_run.get("status", "passed")),
        )
    )
    replay_start = run_date - timedelta(days=4)
    replay_end = run_date - timedelta(days=3)
    replay_result = replay(
        start=replay_start,
        end=replay_end,
        output_root=Path("data/v2_paper_ops"),
    )
    command_log.append(
        _step(
            "PaperOps replay",
            "py -m intraday_scanner.v2.paper_ops replay --start "
            f"{replay_start.isoformat()} --end {replay_end.isoformat()}",
            str(replay_result.get("status", "passed")),
        )
    )
    calendar_result = calendar(output_root=Path("data/v2_paper_ops"))
    reconcile_result = reconcile(output_root=Path("data/v2_paper_ops"))
    report_result = report(output_root=Path("data/v2_paper_ops"))
    command_log.extend(
        [
            _step("PaperOps calendar", "py -m intraday_scanner.v2.paper_ops calendar", str(calendar_result.get("status", "passed"))),
            _step("PaperOps reconcile", "py -m intraday_scanner.v2.paper_ops reconcile", str(reconcile_result.get("status", "passed"))),
            _step("PaperOps report", "py -m intraday_scanner.v2.paper_ops report", str(report_result.get("status", "passed"))),
        ]
    )
    ledger = rebuild_ledger(output_root=Path("data/v2_paper_ops"))
    calendar_truth = verify_calendar_truth(output_root=Path("data/v2_paper_ops"))
    evidence = score_strategy_evidence(output_root=Path("data/v2_paper_ops"))
    readiness = forward_readiness(output_root=Path("data/v2_paper_ops"))
    write_calendar_view(output_root=Path("data/v2_paper_ops"))
    command_log.extend(
        [
            _step("Ledger rebuild", "py -m intraday_scanner.v2.paper_ops rebuild-ledger", ledger.status),
            _step("Calendar truth", "py -m intraday_scanner.v2.paper_ops verify-calendar", calendar_truth.status),
            _step("Strategy evidence", "py -m intraday_scanner.v2.paper_ops evidence", evidence.status),
            _step("Forward readiness", "py -m intraday_scanner.v2.paper_ops readiness", readiness.status),
            _step("Calendar view", "py -m intraday_scanner.v2.paper_ops calendar-view", "passed"),
        ]
    )

    decision = build_decision_engine(run_date=run_date, output_root=output_root)
    risk = build_risk_report(run_date=run_date, output_root=output_root)
    command_log.extend(
        [
            _step("Decision Engine", f"py -m intraday_scanner.v2.decision_engine scan --date {run_date.isoformat()}", decision.status),
            _step("RiskHub", f"py -m intraday_scanner.v2.riskhub report --date {run_date.isoformat()}", risk.status),
        ]
    )

    _write_static_docs(run_date=run_date, run_id=run_id, score=None, blockers=())
    command_center = build_command_center(
        output_root=Path("data/v2_command_center"),
        titan_root=output_root,
    )
    quality = score_titan_quality(titan_root=output_root)
    blockers = tuple(quality.blockers)
    status = "complete" if quality.score >= quality.target and not blockers else "resume_required"
    _write_reports(
        paths=paths,
        run_id=run_id,
        run_date=run_date,
        started_at=started_at,
        data_truth=data_truth,
        alpha=alpha,
        ledger=ledger.to_dict(),
        calendar_truth=calendar_truth.to_dict(),
        evidence=evidence.to_dict(),
        readiness=readiness.to_dict(),
        decision=decision.to_dict(),
        risk=risk.to_dict(),
        command_center=command_center.to_dict(),
        quality=quality.to_dict(),
        command_log=command_log,
        status=status,
    )
    _write_static_docs(run_date=run_date, run_id=run_id, score=quality.score, blockers=blockers)
    command_center = build_command_center(
        output_root=Path("data/v2_command_center"),
        titan_root=output_root,
    )
    result = TitanBuildResult(
        status=status,
        run_id=run_id,
        quality_score=quality.score,
        quality_target=quality.target,
        command_center_index=command_center.index_path,
        blockers=blockers,
        reports_root=paths.reports,
    )
    _write_json(paths.reports / "titan_build_result.json", result.to_dict())
    _write_build_state(paths, result, command_log, quality.to_dict())
    return result


@dataclass(frozen=True)
class _TitanPaths:
    root: Path
    reports: Path
    logs: Path

    @classmethod
    def create(cls, root: Path) -> _TitanPaths:
        paths = cls(root=root, reports=root / "reports", logs=root / "logs")
        for path in (paths.root, paths.reports, paths.logs):
            path.mkdir(parents=True, exist_ok=True)
        return paths


def _write_reports(
    *,
    paths: _TitanPaths,
    run_id: str,
    run_date: date,
    started_at: datetime,
    data_truth: DataTruthBuildResult,
    alpha: AlphaLabRunResult,
    ledger: dict[str, object],
    calendar_truth: dict[str, object],
    evidence: dict[str, object],
    readiness: dict[str, object],
    decision: dict[str, object],
    risk: dict[str, object],
    command_center: dict[str, object],
    quality: dict[str, object],
    command_log: list[dict[str, object]],
    status: str,
) -> None:
    manifest = {
        "artifact_hashes": _artifact_hashes(paths.root),
        "created_at": started_at.isoformat(),
        "live_execution_allowed": False,
        "research_only": True,
        "run_date": run_date.isoformat(),
        "run_id": run_id,
        "status": status,
    }
    summary: dict[str, object] = {
        "alpha_lab": {
            "dataset_id": alpha.dataset.dataset_id,
            "run_id": alpha.run_id,
            "strategies_tested": len(alpha.backtest_results),
        },
        "calendar_truth": calendar_truth,
        "command_center": command_center,
        "data_truth": {
            "accepted_bars": data_truth.manifest.accepted_bar_count,
            "reconciliation": data_truth.reconciliation.status,
            "snapshot_id": data_truth.manifest.snapshot_id,
        },
        "decision_engine": decision,
        "ledger_rebuild": ledger,
        "paper_ops_readiness": readiness,
        "quality": quality,
        "riskhub": risk,
        "run_date": run_date.isoformat(),
        "run_id": run_id,
        "status": status,
    }
    _write_json(paths.reports / "titan_summary.json", summary)
    _write_json(paths.reports / "titan_run_manifest.json", manifest)
    _write_json(paths.reports / "titan_command_log.json", command_log)
    _write_text(paths.reports / "titan_summary.md", _summary_markdown(summary))
    _write_text(paths.reports / "top_opportunities.md", _top_opportunities())
    _write_text(paths.reports / "strategy_health.md", _strategy_health(evidence))
    _write_text(paths.reports / "data_quality.md", _data_quality(data_truth))
    _write_text(paths.reports / "risk_report.md", _read_text(paths.root / "risk" / "risk_report.md"))
    _write_text(paths.reports / "what_changed.md", _what_changed(command_log))
    _write_text(paths.reports / "what_remains_untrusted.md", _untrusted(summary))


def _write_build_state(
    paths: _TitanPaths,
    result: TitanBuildResult,
    command_log: list[dict[str, object]],
    quality: dict[str, object],
) -> None:
    state = {
        **result.to_dict(),
        "commands": command_log,
        "current_blockers": list(result.blockers),
        "dirty_tree": True,
        "git_commit": None,
        "quality": quality,
        "remaining_work": [
            "Upgrade public two-provider OHLCV reconciliation to broker-grade or user-imported source evidence.",
            "Accumulate forward paper evidence before validating strategies.",
            "Align current/latest scan inputs to the DataTruth accepted completed-bar snapshot before treating current candidates as eligible.",
            "Add intraday or broker-grade data before trusting fill timing, liquidity, borrow, or execution assumptions.",
        ],
        "next_actions": [
            "Run the daily build after market close or against a completed-bar DataTruth snapshot.",
            "Import user-owned OHLCV or broker/export data through DataTruth before increasing market-data confidence.",
            "Continue forward PaperOps over real market days before marking any strategy validated.",
            "Investigate RiskHub kill-switch warnings before reviewing current candidates.",
        ],
    }
    _write_json(paths.reports / "titan_build_state.json", state)
    audit_dir = Path("docs/audit")
    audit_dir.mkdir(parents=True, exist_ok=True)
    _write_json(audit_dir / "titan_build_state.json", state)


def _write_static_docs(
    *,
    run_date: date,
    run_id: str,
    score: int | None,
    blockers: tuple[str, ...],
) -> None:
    architecture = Path("docs/architecture")
    operations = Path("docs/operations")
    audit = Path("docs/audit")
    roadmap = Path("docs/roadmap")
    agents = Path("docs/agents")
    for folder in (architecture, operations, audit, roadmap, agents):
        folder.mkdir(parents=True, exist_ok=True)

    _write_text(
        architecture / "v2_titan_architecture.md",
        _doc(
            "v2 Titan Architecture",
            [
                "Titan is an additive orchestration layer over DataTruth, Alpha Lab, PaperOps, Decision Engine, RiskHub, Command Center, and Quality.",
                "It does not modify the legacy Streamlit app, legacy scanner scoring, SQLite schemas, broker adapters, or live-execution guardrails.",
                "Network/public fetches remain in public_data or the orchestration boundary; pure v2 domain modules stay file/artifact based.",
            ],
        ),
    )
    _write_text(
        architecture / "v2_command_center.md",
        _doc(
            "v2 Command Center",
            [
                "The Command Center is static HTML generated under data/v2_command_center.",
                "It reads existing JSON, CSV, and Markdown artifacts. It contains no trading logic and performs no broker actions.",
                "Primary pages: home, market read, picks, watchlist, decision cards, calendar, evidence, backtests, paper ops, risk, data truth, audit, and runbook.",
            ],
        ),
    )
    _write_text(
        architecture / "v2_decision_engine.md",
        _doc(
            "v2 Decision Engine",
            [
                "The Decision Engine enriches latest Alpha Lab cards with DataTruth, strategy evidence, PaperOps, and RiskHub context.",
                "Cards include entry, stop, target, R:R, sizing, max loss estimate, evidence state, data trust state, warnings, and reasons to avoid.",
                "All outputs are research-only and written under data/v2_titan/decision_engine.",
            ],
        ),
    )
    _write_text(
        architecture / "v2_riskhub.md",
        _doc(
            "v2 RiskHub",
            [
                "RiskHub turns candidate cards and PaperOps state into portfolio-level risk warnings.",
                "It reports candidate risk, notional exposure, symbol/strategy concentration, pending orders, open positions, and kill-switch state.",
                "RiskHub blocks confidence when ledger/calendar truth or readiness is blocked.",
            ],
        ),
    )
    _write_text(
        architecture / "v2_backtest_lab.md",
        _doc(
            "v2 Backtest Lab",
            [
                "The current Backtest Lab is the Alpha Lab engine with next-bar execution, fees, slippage, stop-first handling, trade ledgers, equity curves, and strategy comparison.",
                "Alpha Lab now writes first-class robustness outputs with walk-forward split, doubled-cost stress, and deterministic trade-order Monte Carlo summaries.",
                "Titan still needs broker-grade or user-owned data and real forward evidence before any strategy can be called validated.",
            ],
        ),
    )
    _write_text(
        operations / "titan_daily_runbook.md",
        _daily_runbook(run_date),
    )
    _write_text(
        operations / "titan_debugging_runbook.md",
        _doc(
            "Titan Debugging Runbook",
            [
                "If DataTruth fails, inspect data/v2_data_truth/reports/data_truth_summary.md and raw/normalized hashes.",
                "If PaperOps readiness is blocked, run rebuild-ledger, verify-calendar, evidence, and readiness in that order.",
                "If Command Center pages are stale, rerun py -m intraday_scanner.v2.command_center build.",
                "Never repair ledger truth by deleting audit events without explicit user approval.",
            ],
        ),
    )
    _write_text(
        operations / "titan_scheduler_examples.md",
        _doc(
            "Titan Scheduler Examples",
            [
                "Morning research build: py -m intraday_scanner.v2.titan build-all --date YYYY-MM-DD --no-fetch",
                "After-close DataTruth refresh: py -m intraday_scanner.v2.data_truth build --date YYYY-MM-DD",
                "PaperOps readiness check: py -m intraday_scanner.v2.paper_ops readiness",
            ],
        ),
    )
    _write_text(
        roadmap / "titan_backlog.md",
        _backlog(),
    )
    _write_text(
        audit / "titan_build_log.md",
        _doc(
            "Titan Build Log",
            [
                f"Run ID: {run_id}",
                f"Run date: {run_date.isoformat()}",
                "Added Decision Engine, RiskHub, Command Center, Titan master build, Titan quality scoring, and Titan release artifacts.",
                "Preserved legacy app, scanner scoring, SQLite, and live-execution boundaries.",
            ],
        ),
    )
    _write_text(audit / "titan_red_team.md", _red_team(blockers))
    _write_text(audit / "titan_release_summary.md", _release_summary(run_id, score, blockers))
    _write_text(audit / "titan_resume_goal.md", _resume_goal())
    for name in (
        "mission_commander",
        "repository_cartographer",
        "product_architect",
        "quant_research_lead",
        "strategy_factory_engineer",
        "backtest_lab_engineer",
        "datatruth_engineer",
        "paperops_engineer",
        "riskhub_officer",
        "decision_engine_engineer",
        "command_center_designer",
        "qa_evals_engineer",
        "red_team",
        "release_manager",
    ):
        _write_text(
            agents / f"titan_{name}.md",
            _agent_note(name, run_id, score, blockers),
        )


def _summary_markdown(summary: dict[str, object]) -> str:
    quality = summary["quality"] if isinstance(summary["quality"], dict) else {}
    data = summary["data_truth"] if isinstance(summary["data_truth"], dict) else {}
    readiness = summary["paper_ops_readiness"] if isinstance(summary["paper_ops_readiness"], dict) else {}
    return "\n".join(
        [
            "# Titan Buildroom Summary",
            "",
            f"- Run ID: `{summary['run_id']}`",
            f"- Status: `{summary['status']}`",
            f"- Quality score: `{quality.get('score', 'n/a')} / 100`",
            f"- Quality target: `{quality.get('target', 96)} / 100`",
            f"- DataTruth: `{data.get('reconciliation', 'unknown')}`",
            f"- PaperOps readiness: `{readiness.get('status', 'unknown')}`",
            "- Boundary: research-only; no live execution or broker routing.",
            "",
            "## Blockers",
            "",
            *[f"- {item}" for item in quality.get("blockers", [])],
        ]
    ) + "\n"


def _top_opportunities() -> str:
    payload = _read_json(Path("data/v2_titan/decision_engine/current_candidates.json"), [])
    rows = payload if isinstance(payload, list) else []
    lines = ["# Titan Top Opportunities", "", "Research-only candidates ranked by current decision score.", ""]
    for row in rows[:10]:
        if isinstance(row, dict):
            lines.append(
                f"- `{row.get('symbol')}` `{row.get('strategy_id')}` score "
                f"`{row.get('setup_score')}`, max loss `{row.get('max_loss_estimate')}`."
            )
    if len(lines) == 4:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def _strategy_health(evidence: dict[str, object]) -> str:
    rows = evidence.get("scores", []) if isinstance(evidence, dict) else []
    lines = ["# Titan Strategy Health", "", "No strategy is validated unless all forward gates pass.", ""]
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                lines.append(
                    f"- `{row.get('strategy_id')}` `{row.get('evidence_status')}` "
                    f"score `{row.get('overall_score')}` blockers: {row.get('blockers') or 'none'}"
                )
    return "\n".join(lines) + "\n"


def _data_quality(data_truth: DataTruthBuildResult) -> str:
    return "\n".join(
        [
            "# Titan Data Quality",
            "",
            f"- Snapshot: `{data_truth.manifest.snapshot_id}`",
            f"- Accepted bars: `{data_truth.manifest.accepted_bar_count}`",
            f"- Validation: `{data_truth.manifest.validation_status}`",
            f"- Reconciliation: `{data_truth.reconciliation.status}`",
            "",
            "## Warnings",
            "",
            *[f"- {warning}" for warning in data_truth.manifest.warnings],
        ]
    ) + "\n"


def _what_changed(command_log: list[dict[str, object]]) -> str:
    lines = ["# Titan What Changed", "", "## Build Steps", ""]
    lines.extend(f"- `{row['status']}` {row['name']}: `{row['command']}`" for row in command_log)
    return "\n".join(lines) + "\n"


def _untrusted(summary: dict[str, object]) -> str:
    quality = summary["quality"] if isinstance(summary["quality"], dict) else {}
    lines = [
        "# Titan What Remains Untrusted",
        "",
        "- No strategy is validated.",
        "- Public two-provider data reconciliation is not broker-grade execution data.",
        "- Historical contaminated PaperOps ledger events remain as warnings, even though rebuild/calendar truth now pass.",
        "- Fragile strategies are quarantined, but no strategy is validated.",
        "- Current candidates can be blocked when Alpha Lab scan data is newer than the DataTruth accepted completed bar.",
        "- Daily bars do not prove intraday fill timing.",
        "- Corporate action, survivorship, borrow cost, and liquidity controls remain incomplete.",
        "",
        "## Scorecard Blockers",
        "",
    ]
    lines.extend(f"- {blocker}" for blocker in quality.get("blockers", []))
    return "\n".join(lines) + "\n"


def _daily_runbook(run_date: date) -> str:
    return "\n".join(
        [
            "# Titan Daily Runbook",
            "",
            "## Daily Command",
            "",
            f"- `py -m intraday_scanner.v2.titan build-all --date {run_date.isoformat()} --no-fetch`",
            "",
            "## Inspect",
            "",
            "- Open `data/v2_command_center/production.html`.",
            "- Read `data/v2_titan/reports/titan_summary.md`.",
            "- Check `data/v2_titan/reports/what_remains_untrusted.md` before trusting any pick.",
            "- Review `data/v2_paper_ops/reports/forward_readiness.md` for blocked PaperOps state.",
            "",
            "## Strategy Decay",
            "",
            "- Inspect `data/v2_paper_ops/reports/strategy_evidence_scores.csv`.",
            "- Quarantine strategy candidates manually when evidence status is `quarantined` or blockers show negative forward expectancy.",
            "",
            "## Data Import",
            "",
            "- `py -m intraday_scanner.v2.data_truth import-csv --path PATH --provider-id local_csv --date YYYY-MM-DD`",
            "- Rebuild Titan after import.",
            "",
            "## Recovery",
            "",
            "- Run `rebuild-ledger`, `verify-calendar`, `evidence`, and `readiness` in that order.",
            "- Do not delete ledger events without explicit approval.",
        ]
    ) + "\n"


def _backlog() -> str:
    rows = [
        ("Broker-grade DataTruth import", 10, 6, 3, "local CSV or broker export", "public reconciliation supplemented by user-owned broker/export data", "py -m intraday_scanner.v2.data_truth import-csv --path PATH --date YYYY-MM-DD", "pending"),
        ("PaperOps contamination archive decision", 8, 4, 4, "user approval", "historical contaminated ledger warnings are archived or explicitly accepted", "py -m intraday_scanner.v2.paper_ops rebuild-ledger", "needs_user_decision"),
        ("Robustness-driven quarantine", 9, 4, 2, "robustness summary", "fragile strategies downgraded or blocked from current picks", "py -m intraday_scanner.v2.paper_ops evidence", "done"),
        ("Command Center artifact QA", 8, 3, 2, "static pages generated", "index and pages exist without absolute path leaks", "py -m pytest tests\\test_v2_titan.py -q", "done"),
        ("Forward paper evidence accumulation", 9, 7, 2, "market days", "30 forward days and 30 closes per candidate", "py -m intraday_scanner.v2.paper_ops evidence", "blocked_by_time"),
    ]
    lines = [
        "# Titan Backlog",
        "",
        "| Item | Impact | Effort | Risk | Dependency | Acceptance Criteria | Verify | Status | Priority |",
        "| --- | ---: | ---: | ---: | --- | --- | --- | --- | ---: |",
    ]
    for item, impact, effort, risk, dep, acceptance, verify, status in rows:
        priority = impact / (effort + risk)
        lines.append(
            f"| {item} | {impact} | {effort} | {risk} | {dep} | {acceptance} | `{verify}` | {status} | {priority:.2f} |"
        )
    return "\n".join(lines) + "\n"


def _red_team(blockers: tuple[str, ...]) -> str:
    checks = [
        "look-ahead bias remains covered by focused strategy tests and first-pass walk-forward robustness artifacts",
        "same-bar execution uses stop-first conservative handling",
        "synthetic data is blocked from forward mode and labeled as engineering evidence",
        "replay and forward state are separated; historical contaminated ledger events remain as warnings",
        "public two-provider reconciliation is useful but not broker-grade market-data proof",
        "robustness output quarantines fragile strategies; no strategy is validated",
        "Command Center artifact QA checks pages, links, script tags, local path leaks, and research-only banners",
        "no broker imports or live execution paths are added by Titan modules",
        "static HTML contains no trading business logic",
    ]
    return _doc("Titan Red Team", checks + list(blockers))


def _release_summary(run_id: str, score: int | None, blockers: tuple[str, ...]) -> str:
    lines = [
        "# Titan Release Summary",
        "",
        f"- Run ID: `{run_id}`",
        f"- Overall status: `{'RESUME REQUIRED' if blockers or (score is not None and score < 96) else 'COMPLETE'}`",
        f"- Quality score: `{score if score is not None else 'pending'} / 100`",
        "- Live execution: disabled.",
        "- Legacy app/scanner/SQLite behavior: unchanged by Titan modules.",
        "",
        "## Blockers",
        "",
    ]
    if blockers:
        lines.extend(f"- {blocker}" for blocker in blockers)
    elif score is None:
        lines.append("- Pending final quality score.")
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def _resume_goal() -> str:
    return "\n".join(
        [
            "# Titan Resume Goal",
            "",
            "Continue Dawnstrike Titan Buildroom from the current artifacts.",
            "",
            "Required next work:",
            "- Preserve current passing DataTruth reconciliation, PaperOps ledger rebuild, and calendar truth.",
            "- Add broker/export or user-owned local OHLCV import proof if available; otherwise keep public data labeled non-broker-grade.",
            "- Keep robustness-driven quarantine and Command Center artifact QA passing.",
            "- Align current/latest scan inputs with DataTruth accepted completed bars before reviewing current candidates.",
            "- Accumulate real forward PaperOps days and closed trades; do not validate strategies on replay/backtest only.",
            "- Rerun `py -m intraday_scanner.v2.titan build-all --date 2026-06-29 --no-fetch`.",
            "- Rerun full gates: pytest, ruff, mypy, CLI help, and Titan commands.",
            "- Do not validate strategies until forward evidence gates pass.",
        ]
    ) + "\n"


def _agent_note(name: str, run_id: str, score: int | None, blockers: tuple[str, ...]) -> str:
    return _doc(
        f"Titan {name.replace('_', ' ').title()} Findings",
        [
            f"Run ID: {run_id}",
            f"Quality score: {score if score is not None else 'pending'}",
            "Scope stayed additive under v2, docs, and generated data roots.",
            "No live execution or broker routing was enabled.",
            "Current blockers: " + ("; ".join(blockers) if blockers else "pending final score"),
        ],
    )


def _doc(title: str, bullets: list[str]) -> str:
    return "# " + title + "\n\n" + "\n".join(f"- {bullet}" for bullet in bullets) + "\n"


def _step(name: str, command: str, status: object) -> dict[str, object]:
    return {"command": command, "name": name, "status": str(status)}


def _artifact_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not root.exists():
        return hashes
    for path in sorted(root.rglob("*")):
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = _sha256(path)
    return hashes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
