# ruff: noqa: E501
"""Static HTML Command Center generated from Titan artifacts."""

from __future__ import annotations

import csv
import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

REQUIRED_PAGES = (
    "index.html",
    "production.html",
    "market_read.html",
    "picks.html",
    "watchlist.html",
    "decision_cards.html",
    "strategy_calendar.html",
    "strategy_calendar_forward.html",
    "strategy_decay.html",
    "strategy_evidence.html",
    "backtests.html",
    "paper_ops.html",
    "forward_evidence.html",
    "frozen_picks.html",
    "risk.html",
    "riskhub_daily.html",
    "daily_run_status.html",
    "evidence_integrity.html",
    "shadow_replay.html",
    "data_truth.html",
    "audit.html",
    "runbook.html",
    "omega_sentinel.html",
    "forward_trial.html",
    "daily_status.html",
    "alerts.html",
    "artifact_index.html",
    "fill_truth.html",
    "pending_orders.html",
    "execution_models.html",
    "fill_certainty.html",
    "evidence_commit.html",
    "commit_proposals.html",
    "pending_divergence.html",
    "real_intraday_readiness.html",
    "real_intraday.html",
    "intraday_reconciliation.html",
    "real_evidence_trial.html",
    "import_readiness.html",
    "intraday_import_templates.html",
    "autodata.html",
    "provider_readiness.html",
    "provider_fetches.html",
    "provider_reconciliation.html",
    "autodata_pending_orders.html",
    "autodata_filltruth.html",
    "learning_foundry.html",
    "market_regimes.html",
    "feature_store.html",
    "news_events.html",
    "challenger_strategies.html",
    "model_lab.html",
    "daily_lessons.html",
    "promotion_review.html",
    "autonomous_runner.html",
    "task_scheduler.html",
    "scheduler_status.html",
    "watchdog.html",
    "missed_runs.html",
    "telegram_intel.html",
    "telegram_messages.html",
    "telegram_readiness.html",
    "message_quality.html",
    "market_masters.html",
    "market_masters_sources.html",
    "methodology_taxonomy.html",
    "strategy_primitives.html",
    "market_masters_challengers.html",
    "market_masters_shadow.html",
    "market_masters_evals.html",
    "market_masters_lessons.html",
)


@dataclass(frozen=True)
class CommandCenterResult:
    status: str
    output_root: Path
    index_path: Path
    pages: tuple[Path, ...]
    qa_report_path: Path
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "index_path": self.index_path.as_posix(),
            "output_root": self.output_root.as_posix(),
            "pages": [path.as_posix() for path in self.pages],
            "qa_report_path": self.qa_report_path.as_posix(),
            "status": self.status,
            "warnings": list(self.warnings),
        }


def build_command_center(
    *,
    output_root: Path = Path("data/v2_command_center"),
    titan_root: Path = Path("data/v2_titan"),
    alpha_root: Path = Path("data/v2_alpha_lab"),
    data_truth_root: Path = Path("data/v2_data_truth"),
    paper_ops_root: Path = Path("data/v2_paper_ops"),
    forward_root: Path = Path("data/v2_forward_evidence"),
    sentinel_root: Path = Path("data/v2_omega_sentinel"),
    fill_truth_root: Path = Path("data/v2_fill_truth"),
    evidence_commit_root: Path = Path("data/v2_evidence_commit"),
    real_intraday_root: Path = Path("data/v2_real_intraday"),
    autodata_root: Path = Path("data/v2_autodata"),
    learning_root: Path = Path("data/v2_learning_foundry"),
    autonomous_root: Path = Path("data/v2_autonomous_runner"),
    telegram_root: Path = Path("data/v2_telegram_intel"),
    market_masters_root: Path = Path("data/v2_market_masters"),
) -> CommandCenterResult:
    output_root.mkdir(parents=True, exist_ok=True)
    data = _load_center_data(
        titan_root=titan_root,
        alpha_root=alpha_root,
        data_truth_root=data_truth_root,
        paper_ops_root=paper_ops_root,
        forward_root=forward_root,
        sentinel_root=sentinel_root,
        fill_truth_root=fill_truth_root,
        evidence_commit_root=evidence_commit_root,
        real_intraday_root=real_intraday_root,
        autodata_root=autodata_root,
        learning_root=learning_root,
        autonomous_root=autonomous_root,
        telegram_root=telegram_root,
        market_masters_root=market_masters_root,
    )
    pages = {
        "index.html": _home(data),
        "production.html": _production_page(output_root=output_root),
        "market_read.html": _artifact_page("Market Read", data["alpha_summary"], data),
        "picks.html": _table_page("Current Picks", data["decision_cards"], data),
        "watchlist.html": _table_page("Watchlist", data["watchlist"], data),
        "decision_cards.html": _table_page("Decision Cards", data["decision_cards"], data),
        "strategy_calendar.html": _artifact_page("Strategy Calendar", data["calendar_summary"], data),
        "strategy_calendar_forward.html": _artifact_page(
            "Forward Strategy Calendar", data["forward_calendar_summary"], data
        ),
        "strategy_decay.html": _table_page("Strategy Decay", data["strategy_decay"], data),
        "strategy_evidence.html": _artifact_page(
            "Strategy Evidence", data["strategy_evidence_summary"], data
        ),
        "backtests.html": _table_page("Backtests", data["strategy_comparison"], data),
        "paper_ops.html": _artifact_page("PaperOps", data["paper_ops_summary"], data),
        "forward_evidence.html": _artifact_page("Forward Evidence", data["forward_summary"], data),
        "frozen_picks.html": _table_page("Frozen Picks", data["frozen_picks"], data),
        "risk.html": _artifact_page("RiskHub", data["risk_report"], data),
        "riskhub_daily.html": _artifact_page("RiskHub Daily", data["riskhub_daily"], data),
        "daily_run_status.html": _artifact_page("Daily Run Status", data["daily_run_status"], data),
        "evidence_integrity.html": _artifact_page(
            "Evidence Integrity", data["evidence_integrity"], data
        ),
        "shadow_replay.html": _artifact_page("Shadow Replay", data["shadow_replay"], data),
        "data_truth.html": _artifact_page("DataTruth", data["data_truth_summary"], data),
        "audit.html": _artifact_page("Audit", data["audit_summary"], data),
        "runbook.html": _artifact_page("Runbook", data["runbook"], data),
        "omega_sentinel.html": _artifact_page(
            "OMEGA Sentinel",
            data["sentinel_report"],
            data,
        ),
        "forward_trial.html": _artifact_page(
            "Forward Trial",
            data["sentinel_trial"],
            data,
        ),
        "daily_status.html": _artifact_page(
            "Daily Status",
            data["sentinel_status"],
            data,
        ),
        "alerts.html": _artifact_page("Alerts", data["sentinel_alert"], data),
        "artifact_index.html": _table_page(
            "Artifact Index",
            data["sentinel_artifacts"],
            data,
        ),
        "fill_truth.html": _artifact_page("FillTruth", data["fill_truth_summary"], data),
        "pending_orders.html": _table_page(
            "Pending Orders",
            data["fill_truth_pending"],
            data,
        ),
        "execution_models.html": _table_page(
            "Execution Models",
            data["fill_truth_execution_models"],
            data,
        ),
        "fill_certainty.html": _artifact_page(
            "Fill Certainty",
            data["fill_truth_certainty"],
            data,
        ),
        "evidence_commit.html": _artifact_page(
            "Evidence CommitBridge",
            data["evidence_commit_summary"],
            data,
        ),
        "commit_proposals.html": _table_page(
            "Commit Proposals",
            data["commit_proposals"],
            data,
        ),
        "pending_divergence.html": _artifact_page(
            "Pending Divergence",
            data["pending_divergence"],
            data,
        ),
        "real_intraday_readiness.html": _artifact_page(
            "Real Intraday Readiness",
            data["real_intraday_readiness"],
            data,
        ),
        "real_intraday.html": _artifact_page(
            "Real Intraday Intake",
            data["real_intraday_summary"],
            data,
        ),
        "intraday_reconciliation.html": _artifact_page(
            "Intraday Reconciliation",
            data["intraday_reconciliation"],
            data,
        ),
        "real_evidence_trial.html": _artifact_page(
            "Real Evidence Trial",
            data["real_evidence_trial"],
            data,
        ),
        "import_readiness.html": _artifact_page(
            "Import Readiness",
            data["import_readiness"],
            data,
        ),
        "intraday_import_templates.html": _artifact_page(
            "Intraday Import Templates",
            data["intraday_import_templates"],
            data,
        ),
        "autodata.html": _artifact_page("AutoData", data["autodata_summary"], data),
        "provider_readiness.html": _artifact_page(
            "Provider Readiness",
            data["autodata_provider_readiness"],
            data,
        ),
        "provider_fetches.html": _artifact_page(
            "Provider Fetches",
            data["autodata_fetch_pending"],
            data,
        ),
        "provider_reconciliation.html": _artifact_page(
            "Provider Reconciliation",
            data["autodata_reconciliation"],
            data,
        ),
        "autodata_pending_orders.html": _artifact_page(
            "AutoData Pending Orders",
            data["autodata_trial_day"],
            data,
        ),
        "autodata_filltruth.html": _artifact_page(
            "AutoData FillTruth",
            data["autodata_filltruth"],
            data,
        ),
        "learning_foundry.html": _artifact_page(
            "Learning Foundry",
            data["learning_foundry_summary"],
            data,
        ),
        "market_regimes.html": _artifact_page(
            "Market Regimes",
            data["learning_market_regimes"],
            data,
        ),
        "feature_store.html": _artifact_page(
            "Feature Store",
            data["learning_feature_store"],
            data,
        ),
        "news_events.html": _artifact_page(
            "News Events",
            data["learning_news_events"],
            data,
        ),
        "challenger_strategies.html": _table_page(
            "Challenger Strategies",
            data["learning_challengers"],
            data,
        ),
        "model_lab.html": _artifact_page(
            "Model Lab",
            data["learning_model_lab"],
            data,
        ),
        "daily_lessons.html": _artifact_page(
            "Daily Lessons",
            data["learning_daily_lessons"],
            data,
        ),
        "promotion_review.html": _artifact_page(
            "Promotion Review",
            data["learning_promotion_review"],
            data,
        ),
        "autonomous_runner.html": _artifact_page(
            "Autonomous Runner",
            data["autonomous_runner_status"],
            data,
        ),
        "task_scheduler.html": _table_page(
            "Task Scheduler",
            data["autonomous_tasks"],
            data,
        ),
        "scheduler_status.html": _artifact_page(
            "Scheduler Status",
            data["autonomous_scheduler_status"],
            data,
        ),
        "watchdog.html": _artifact_page(
            "Watchdog",
            data["autonomous_watchdog"],
            data,
        ),
        "missed_runs.html": _table_page(
            "Missed Runs",
            data["autonomous_missed_runs"],
            data,
        ),
        "telegram_intel.html": _artifact_page(
            "Telegram Intelligence",
            data["telegram_report"],
            data,
        ),
        "telegram_messages.html": _telegram_messages_page(data),
        "telegram_readiness.html": _artifact_page(
            "Telegram Readiness",
            data["telegram_readiness"],
            data,
        ),
        "message_quality.html": _artifact_page(
            "Message Quality",
            data["telegram_quality"],
            data,
        ),
        "market_masters.html": _artifact_page(
            "Market Masters",
            data["market_masters_report"],
            data,
        ),
        "market_masters_sources.html": _table_page(
            "Market Masters Sources",
            data["market_masters_sources"],
            data,
        ),
        "methodology_taxonomy.html": _table_page(
            "Methodology Taxonomy",
            data["market_masters_methodologies"],
            data,
        ),
        "strategy_primitives.html": _table_page(
            "Strategy Primitives",
            data["market_masters_primitives"],
            data,
        ),
        "market_masters_challengers.html": _table_page(
            "Market Masters Challengers",
            data["market_masters_challengers"],
            data,
        ),
        "market_masters_shadow.html": _table_page(
            "Market Masters Shadow",
            data["market_masters_shadow"],
            data,
        ),
        "market_masters_evals.html": _table_page(
            "Market Masters Evals",
            data["market_masters_evals"],
            data,
        ),
        "market_masters_lessons.html": _artifact_page(
            "Market Masters Lessons",
            data["market_masters_lessons"],
            data,
        ),
    }
    written: list[Path] = []
    for name, body in pages.items():
        path = output_root / name
        path.write_text(_shell(title=name.removesuffix(".html"), body=body), encoding="utf-8")
        written.append(path)
    qa_report = _write_command_center_qa(output_root=output_root, pages=tuple(written))
    qa_warnings = tuple(str(item) for item in _list_any(qa_report.get("warnings")))
    result = CommandCenterResult(
        status="passed" if qa_report.get("status") == "passed" else "passed_with_warnings",
        output_root=output_root,
        index_path=output_root / "index.html",
        pages=tuple(written),
        qa_report_path=output_root / "command_center_qa.json",
        warnings=tuple(str(item) for item in _list_any(data["warnings"])) + qa_warnings,
    )
    _write_json(output_root / "command_center_manifest.json", result.to_dict())
    return result


def _load_center_data(
    *,
    titan_root: Path,
    alpha_root: Path,
    data_truth_root: Path,
    paper_ops_root: Path,
    forward_root: Path,
    sentinel_root: Path,
    fill_truth_root: Path,
    evidence_commit_root: Path,
    real_intraday_root: Path,
    autodata_root: Path,
    learning_root: Path,
    autonomous_root: Path,
    telegram_root: Path,
    market_masters_root: Path,
) -> dict[str, object]:
    decision_cards = _read_json(titan_root / "decision_engine" / "decision_cards.json", [])
    watchlist = _read_json(titan_root / "decision_engine" / "watchlist.json", [])
    risk_report = _read_text(titan_root / "risk" / "risk_report.md")
    alpha_summary = _read_text(alpha_root / "reports" / "alpha_lab_summary.md")
    data_truth_summary = _read_text(data_truth_root / "reports" / "data_truth_summary.md")
    paper_ops_summary = _read_text(paper_ops_root / "reports" / "paper_ops_summary.md")
    calendar_summary = _read_text(paper_ops_root / "calendar" / "calendar_summary.md")
    strategy_evidence_summary = _read_text(
        paper_ops_root / "reports" / "strategy_evidence_summary.md"
    )
    strategy_comparison = _read_json(alpha_root / "reports" / "strategy_comparison.json", [])
    audit_summary = _read_text(Path("docs/audit/titan_release_summary.md"))
    runbook = _read_text(Path("docs/operations/titan_daily_runbook.md"))
    forward_summary = _read_text(Path("docs/audit/forward_autopilot_summary.md"))
    forward_calendar_summary = _read_text(
        forward_root / "calendar" / "strategy_calendar_summary.md"
    )
    strategy_decay = _read_csv(forward_root / "calendar" / "strategy_decay_report.csv")
    frozen_picks = _latest_frozen_rows(forward_root / "frozen_picks")
    riskhub_daily = _read_text(forward_root / "reports" / "riskhub_daily.md")
    daily_run_status = _read_text(_latest_file(forward_root / "reports" / "daily", "*.md"))
    evidence_integrity = _read_text(forward_root / "reconciliation" / "evidence_integrity.md")
    shadow_replay = _read_text(
        forward_root / "shadow_replay" / "reports" / "shadow_replay_summary.md"
    )
    sentinel_report = _read_text(
        sentinel_root / "reports" / "latest_omega_sentinel_report.md"
    )
    sentinel_trial = _read_text(sentinel_root / "trial" / "forward_trial_status.md")
    sentinel_status = _read_text(sentinel_root / "status" / "latest_status.md")
    sentinel_alert = _read_text(sentinel_root / "alerts" / "latest_alert.md")
    sentinel_index = _read_json(sentinel_root / "retention" / "artifact_index.json", {})
    sentinel_artifacts = (
        sentinel_index.get("rows")
        if isinstance(sentinel_index, dict) and isinstance(sentinel_index.get("rows"), list)
        else []
    )
    fill_truth_summary = _read_text(fill_truth_root / "reports" / "filltruth_summary.md")
    fill_truth_resolution = _read_json(
        fill_truth_root / "reports" / "pending_resolution_latest.json",
        {},
    )
    fill_truth_comparison = _read_json(
        fill_truth_root / "comparisons" / "execution_model_comparison.json",
        {},
    )
    fill_truth_pending = (
        fill_truth_resolution.get("decisions")
        if isinstance(fill_truth_resolution, dict)
        and isinstance(fill_truth_resolution.get("decisions"), list)
        else []
    )
    fill_truth_execution_models = (
        fill_truth_comparison.get("rows")
        if isinstance(fill_truth_comparison, dict)
        and isinstance(fill_truth_comparison.get("rows"), list)
        else []
    )
    fill_truth_certainty = _fill_truth_certainty_text(fill_truth_resolution)
    evidence_commit_summary = _read_text(
        evidence_commit_root / "reports" / "evidence_commit_summary.md"
    )
    commit_proposals_payload = _read_json(
        evidence_commit_root / "reports" / "latest_commit_proposals.json",
        {},
    )
    commit_proposals = (
        commit_proposals_payload.get("proposals")
        if isinstance(commit_proposals_payload, dict)
        and isinstance(commit_proposals_payload.get("proposals"), list)
        else []
    )
    pending_divergence = _read_text(
        evidence_commit_root / "reports" / "pending_divergence_latest.md"
    )
    real_intraday_readiness = _read_text(
        evidence_commit_root / "reports" / "real_intraday_readiness.md"
    )
    real_intraday_summary = _read_text(
        real_intraday_root / "reports" / "real_intraday_summary.md"
    )
    intraday_reconciliation = _read_text(
        real_intraday_root / "reports" / "intraday_daily_reconciliation_latest.md"
    )
    real_evidence_trial = _read_text(
        real_intraday_root / "reports" / "trial_day_latest.md"
    )
    import_readiness = _read_text(real_intraday_root / "reports" / "import_readiness.md")
    intraday_import_templates = _read_text(real_intraday_root / "reports" / "import_templates.md")
    autodata_summary = _read_text(autodata_root / "reports" / "autodata_summary.md")
    autodata_provider_readiness = _read_text(autodata_root / "reports" / "provider_readiness.md")
    autodata_fetch_pending = _read_text(autodata_root / "reports" / "fetch_pending_latest.md")
    autodata_reconciliation = _read_text(
        autodata_root / "reports" / "provider_reconciliation_latest.md"
    )
    autodata_trial_day = _read_text(autodata_root / "reports" / "autodata_trial_day_latest.md")
    autodata_filltruth = _read_text(autodata_root / "reports" / "autodata_filltruth_latest.md")
    learning_foundry_summary = _read_text(learning_root / "reports" / "learning_foundry_summary.md")
    learning_market_regimes = _read_text(learning_root / "reports" / "market_regimes.md")
    learning_feature_store = _read_text(learning_root / "reports" / "feature_store_summary.md")
    learning_news_events = _read_text(learning_root / "news" / "news_readiness.md")
    learning_challengers_payload = _read_json(
        learning_root / "candidates" / "challenger_registry.json",
        {},
    )
    learning_challengers = (
        learning_challengers_payload.get("candidates")
        if isinstance(learning_challengers_payload, dict)
        and isinstance(learning_challengers_payload.get("candidates"), list)
        else []
    )
    learning_model_lab = _read_text(learning_root / "reports" / "model_training_summary.md")
    learning_daily_lessons = _read_text(_latest_file(learning_root / "lessons", "*.md"))
    learning_promotion_review = _read_text(learning_root / "reports" / "promotion_review.md")
    autonomous_runner_status = _read_text(
        autonomous_root / "reports" / "autonomous_runner_status.md"
    )
    autonomous_status_payload = _read_json(
        autonomous_root / "status" / "latest_status.json",
        {},
    )
    autonomous_tasks = (
        autonomous_status_payload.get("tasks")
        if isinstance(autonomous_status_payload, dict)
        and isinstance(autonomous_status_payload.get("tasks"), list)
        else []
    )
    autonomous_scheduler_status = _read_text(Path("data/v2_scheduler/status/latest_status.md"))
    autonomous_watchdog = _read_text(autonomous_root / "health" / "watchdog_latest.md")
    autonomous_missed_runs = (
        autonomous_status_payload.get("missed_runs", {}).get("rows")
        if isinstance(autonomous_status_payload, dict)
        and isinstance(autonomous_status_payload.get("missed_runs"), dict)
        and isinstance(autonomous_status_payload.get("missed_runs", {}).get("rows"), list)
        else []
    )
    telegram_report = _read_text(telegram_root / "reports" / "report_latest.md")
    telegram_readiness = _read_text(telegram_root / "status" / "latest_readiness.md")
    telegram_quality = _read_text(telegram_root / "reports" / "message_quality_latest.md")
    telegram_message_payload = _read_json(telegram_root / "messages" / "latest_message.json", {})
    telegram_send_payload = _read_json(telegram_root / "reports" / "send_latest.json", {})
    market_masters_report = _read_text(market_masters_root / "reports" / "report_latest.md")
    market_masters_source_payload = _read_json(
        market_masters_root / "source_register" / "source_register.json",
        {},
    )
    market_masters_sources = (
        market_masters_source_payload.get("rows")
        if isinstance(market_masters_source_payload, dict)
        and isinstance(market_masters_source_payload.get("rows"), list)
        else []
    )
    market_masters_methodology_payload = _read_json(
        market_masters_root / "methodologies" / "methodology_taxonomy.json",
        {},
    )
    market_masters_methodologies = (
        market_masters_methodology_payload.get("methodologies")
        if isinstance(market_masters_methodology_payload, dict)
        and isinstance(market_masters_methodology_payload.get("methodologies"), list)
        else []
    )
    market_masters_primitive_payload = _read_json(
        market_masters_root / "primitives" / "strategy_primitives.json",
        {},
    )
    market_masters_primitives = (
        market_masters_primitive_payload.get("primitives")
        if isinstance(market_masters_primitive_payload, dict)
        and isinstance(market_masters_primitive_payload.get("primitives"), list)
        else []
    )
    market_masters_challenger_payload = _read_json(
        market_masters_root / "candidates" / "challenger_registry.json",
        {},
    )
    market_masters_challengers = (
        market_masters_challenger_payload.get("challengers")
        if isinstance(market_masters_challenger_payload, dict)
        and isinstance(market_masters_challenger_payload.get("challengers"), list)
        else []
    )
    market_masters_shadow_payload = _read_json(
        _latest_file(market_masters_root / "shadow_runs", "*_shadow_results.json"),
        {},
    )
    market_masters_shadow = (
        market_masters_shadow_payload.get("rows")
        if isinstance(market_masters_shadow_payload, dict)
        and isinstance(market_masters_shadow_payload.get("rows"), list)
        else []
    )
    market_masters_eval_payload = _read_json(
        _latest_file(market_masters_root / "evals", "*_eval.json"),
        {},
    )
    market_masters_evals = (
        market_masters_eval_payload.get("rows")
        if isinstance(market_masters_eval_payload, dict)
        and isinstance(market_masters_eval_payload.get("rows"), list)
        else []
    )
    market_masters_lessons = _read_text(Path("data/v2_learning_foundry/reports/market_masters_sync.md"))
    warnings = []
    if "single_provider" in data_truth_summary:
        warnings.append("DataTruth remains single-provider.")
    readiness = _read_json(paper_ops_root / "reports" / "forward_readiness.json", {})
    if isinstance(readiness, dict) and str(readiness.get("status", "")).lower() == "blocked":
        warnings.append("PaperOps readiness is blocked.")
    return {
        "alpha_summary": alpha_summary,
        "audit_summary": audit_summary,
        "calendar_summary": calendar_summary,
        "data_truth_summary": data_truth_summary,
        "decision_cards": decision_cards if isinstance(decision_cards, list) else [],
        "daily_run_status": daily_run_status,
        "evidence_integrity": evidence_integrity,
        "forward_calendar_summary": forward_calendar_summary,
        "forward_summary": forward_summary,
        "fill_truth_certainty": fill_truth_certainty,
        "fill_truth_execution_models": fill_truth_execution_models,
        "fill_truth_pending": fill_truth_pending,
        "fill_truth_summary": fill_truth_summary,
        "commit_proposals": commit_proposals,
        "evidence_commit_summary": evidence_commit_summary,
        "frozen_picks": frozen_picks,
        "paper_ops_summary": paper_ops_summary,
        "risk_report": risk_report,
        "riskhub_daily": riskhub_daily,
        "pending_divergence": pending_divergence,
        "real_intraday_readiness": real_intraday_readiness,
        "real_intraday_summary": real_intraday_summary,
        "intraday_reconciliation": intraday_reconciliation,
        "real_evidence_trial": real_evidence_trial,
        "import_readiness": import_readiness,
        "intraday_import_templates": intraday_import_templates,
        "autodata_summary": autodata_summary,
        "autonomous_missed_runs": autonomous_missed_runs,
        "autonomous_runner_status": autonomous_runner_status,
        "autonomous_scheduler_status": autonomous_scheduler_status,
        "autonomous_tasks": autonomous_tasks,
        "autonomous_watchdog": autonomous_watchdog,
        "telegram_message_payload": telegram_message_payload if isinstance(telegram_message_payload, dict) else {},
        "telegram_quality": telegram_quality,
        "telegram_readiness": telegram_readiness,
        "telegram_report": telegram_report,
        "telegram_send_payload": telegram_send_payload if isinstance(telegram_send_payload, dict) else {},
        "market_masters_challengers": market_masters_challengers,
        "market_masters_evals": market_masters_evals,
        "market_masters_lessons": market_masters_lessons,
        "market_masters_methodologies": market_masters_methodologies,
        "market_masters_primitives": market_masters_primitives,
        "market_masters_report": market_masters_report,
        "market_masters_shadow": market_masters_shadow,
        "market_masters_sources": market_masters_sources,
        "autodata_provider_readiness": autodata_provider_readiness,
        "autodata_fetch_pending": autodata_fetch_pending,
        "autodata_reconciliation": autodata_reconciliation,
        "autodata_trial_day": autodata_trial_day,
        "autodata_filltruth": autodata_filltruth,
        "learning_foundry_summary": learning_foundry_summary,
        "learning_market_regimes": learning_market_regimes,
        "learning_feature_store": learning_feature_store,
        "learning_news_events": learning_news_events,
        "learning_challengers": learning_challengers,
        "learning_model_lab": learning_model_lab,
        "learning_daily_lessons": learning_daily_lessons,
        "learning_promotion_review": learning_promotion_review,
        "runbook": runbook,
        "sentinel_alert": sentinel_alert,
        "sentinel_artifacts": sentinel_artifacts,
        "sentinel_report": sentinel_report,
        "sentinel_status": sentinel_status,
        "sentinel_trial": sentinel_trial,
        "shadow_replay": shadow_replay,
        "strategy_decay": strategy_decay,
        "strategy_comparison": strategy_comparison if isinstance(strategy_comparison, list) else [],
        "strategy_evidence_summary": strategy_evidence_summary,
        "warnings": warnings,
        "watchlist": watchlist if isinstance(watchlist, list) else [],
    }


def _fill_truth_certainty_text(payload: object) -> str:
    if not isinstance(payload, dict) or not payload:
        return "# Fill Certainty\n\nArtifact not generated yet.\n"
    summary = payload.get("fill_certainty_summary", {})
    warnings = payload.get("warnings", [])
    lines = [
        "# Fill Certainty",
        "",
        f"- Status: `{payload.get('status', 'unknown')}`",
        f"- Run date: `{payload.get('run_date', 'unknown')}`",
        f"- Pending orders inspected: `{payload.get('pending_orders_inspected', 0)}`",
        f"- Fills resolved: `{payload.get('fills_resolved', 0)}`",
        f"- Pending after resolution: `{payload.get('pending_orders_after_resolution', 0)}`",
        f"- Summary: `{json.dumps(summary, sort_keys=True)}`",
        "",
        "## Warnings",
        "",
    ]
    if isinstance(warnings, list) and warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def _home(data: dict[str, object]) -> str:
    cards = data["decision_cards"] if isinstance(data["decision_cards"], list) else []
    watchlist = data["watchlist"] if isinstance(data["watchlist"], list) else []
    frozen = data["frozen_picks"] if isinstance(data["frozen_picks"], list) else []
    warnings = data["warnings"] if isinstance(data["warnings"], list) else []
    return (
        "<section><h1>Dawnstrike v2 Titan Command Center</h1>"
        "<p>Local research and paper-ops dashboard. Research-only; no live execution.</p>"
        "<div class='production-callout'>"
        "<h2>Production operator view</h2>"
        "<p>Command Center X2 is the production story interface for daily review. "
        "This legacy Command Center remains available for artifact drill-downs.</p>"
        "<p><a class='primary-link' href='production.html'>Open production Command Center X2</a></p>"
        "</div>"
        "<div class='metrics'>"
        f"<div><strong>{len(cards)}</strong><span>Current Picks</span></div>"
        f"<div><strong>{len(watchlist)}</strong><span>Watchlist Rows</span></div>"
        f"<div><strong>{len(frozen)}</strong><span>Frozen Rows</span></div>"
        f"<div><strong>{len(warnings)}</strong><span>Warnings</span></div>"
        "</div>"
        "<h2>Warnings</h2>"
        + _list_html([str(item) for item in warnings] or ["None."])
        + "</section>"
    )


def _production_page(*, output_root: Path) -> str:
    x2_index = output_root.parent / "v2_command_center_x2" / "index.html"
    x_index = output_root.parent / "v2_command_center_x" / "index.html"
    if x2_index.exists():
        x2_entry = (
            "<p><a class='primary-link' href='../v2_command_center_x2/index.html'>"
            "Open Command Center X2</a></p>"
        )
    else:
        x2_entry = (
            "<p>Command Center X2 has not been built next to this output root yet.</p>"
            "<pre>py -m intraday_scanner.v2.command_center_x2 demo</pre>"
        )
    x_entry = (
        "<li><a href='../v2_command_center_x/index.html'>Command Center X functional baseline</a></li>"
        if x_index.exists()
        else "<li>Command Center X functional baseline: build with <code>py -m intraday_scanner.v2.command_center_x build</code>.</li>"
    )
    return (
        "<section><h1>Production Command Center</h1>"
        "<p>Command Center X2 is the default local production operator view. "
        "It is story-first, artifact-wired, research-only, and paper-only.</p>"
        + x2_entry
        + "<h2>Fallbacks remain available</h2>"
        "<ul>"
        "<li><a href='index.html'>Legacy Command Center artifact view</a></li>"
        + x_entry
        + "</ul>"
        "<h2>Safety boundary</h2>"
        "<p>No live trading controls, broker routing, provider calls, Telegram sends, or PaperOps mutations are available from this production entry.</p>"
        "</section>"
    )


def _artifact_page(title: str, markdown_text: object, data: dict[str, object]) -> str:
    del data
    return f"<section><h1>{_esc(title)}</h1>{_markdownish(str(markdown_text))}</section>"


def _table_page(title: str, rows_object: object, data: dict[str, object]) -> str:
    del data
    rows = rows_object if isinstance(rows_object, list) else []
    return f"<section><h1>{_esc(title)}</h1>{_table(rows)}</section>"


def _telegram_messages_page(data: dict[str, object]) -> str:
    latest = data["telegram_message_payload"] if isinstance(data["telegram_message_payload"], dict) else {}
    send_payload = data["telegram_send_payload"] if isinstance(data["telegram_send_payload"], dict) else {}
    rows = [
        {"field": "latest_kind", "value": latest.get("kind", "missing")},
        {"field": "quality_score", "value": latest.get("quality_score", "missing")},
        {"field": "send_status", "value": send_payload.get("send_status", "not_sent")},
        {"field": "message_hash", "value": latest.get("message_hash", "missing")},
    ]
    text = str(latest.get("text", "No Telegram draft generated yet."))
    return (
        "<section><h1>Telegram Messages</h1>"
        + _table(rows)
        + "<h2>Latest Message</h2><pre>"
        + _esc(text)
        + "</pre></section>"
    )


def _shell(*, title: str, body: str) -> str:
    nav = [
        ("Production X2", "production.html"),
        ("Home", "index.html"),
        ("Market", "market_read.html"),
        ("Picks", "picks.html"),
        ("Watchlist", "watchlist.html"),
        ("Cards", "decision_cards.html"),
        ("Calendar", "strategy_calendar.html"),
        ("Forward Calendar", "strategy_calendar_forward.html"),
        ("Decay", "strategy_decay.html"),
        ("Evidence", "strategy_evidence.html"),
        ("Backtests", "backtests.html"),
        ("PaperOps", "paper_ops.html"),
        ("Forward Evidence", "forward_evidence.html"),
        ("Frozen", "frozen_picks.html"),
        ("Risk", "risk.html"),
        ("RiskHub Daily", "riskhub_daily.html"),
        ("Daily Run", "daily_run_status.html"),
        ("Integrity", "evidence_integrity.html"),
        ("Shadow Replay", "shadow_replay.html"),
        ("DataTruth", "data_truth.html"),
        ("Audit", "audit.html"),
        ("Runbook", "runbook.html"),
        ("Sentinel", "omega_sentinel.html"),
        ("Trial", "forward_trial.html"),
        ("Daily Status", "daily_status.html"),
        ("Alerts", "alerts.html"),
        ("Artifacts", "artifact_index.html"),
        ("FillTruth", "fill_truth.html"),
        ("Pending", "pending_orders.html"),
        ("Exec Models", "execution_models.html"),
        ("Certainty", "fill_certainty.html"),
        ("CommitBridge", "evidence_commit.html"),
        ("Proposals", "commit_proposals.html"),
        ("Divergence", "pending_divergence.html"),
        ("Intraday Ready", "real_intraday_readiness.html"),
        ("Real Intraday", "real_intraday.html"),
        ("Intraday Recon", "intraday_reconciliation.html"),
        ("Evidence Trial", "real_evidence_trial.html"),
        ("Import Ready", "import_readiness.html"),
        ("Templates", "intraday_import_templates.html"),
        ("AutoData", "autodata.html"),
        ("Providers", "provider_readiness.html"),
        ("Fetches", "provider_fetches.html"),
        ("Provider Recon", "provider_reconciliation.html"),
        ("Auto Pending", "autodata_pending_orders.html"),
        ("Auto FillTruth", "autodata_filltruth.html"),
        ("Learning", "learning_foundry.html"),
        ("Regimes", "market_regimes.html"),
        ("Features", "feature_store.html"),
        ("News", "news_events.html"),
        ("Challengers", "challenger_strategies.html"),
        ("Model Lab", "model_lab.html"),
        ("Lessons", "daily_lessons.html"),
        ("Promotion", "promotion_review.html"),
        ("Autonomous", "autonomous_runner.html"),
        ("Tasks", "task_scheduler.html"),
        ("Scheduler", "scheduler_status.html"),
        ("Watchdog", "watchdog.html"),
        ("Missed Runs", "missed_runs.html"),
        ("Telegram", "telegram_intel.html"),
        ("Telegram Msgs", "telegram_messages.html"),
        ("Telegram Ready", "telegram_readiness.html"),
        ("Msg Quality", "message_quality.html"),
        ("Market Masters", "market_masters.html"),
        ("MM Sources", "market_masters_sources.html"),
        ("MM Methods", "methodology_taxonomy.html"),
        ("MM Primitives", "strategy_primitives.html"),
        ("MM Challengers", "market_masters_challengers.html"),
        ("MM Shadow", "market_masters_shadow.html"),
        ("MM Evals", "market_masters_evals.html"),
        ("MM Lessons", "market_masters_lessons.html"),
    ]
    nav_html = "".join(f"<a href='{href}'>{label}</a>" for label, href in nav)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dawnstrike Titan - {_esc(title)}</title>
<style>
body {{ margin: 0; font-family: Arial, sans-serif; color: #20242a; background: #f7f8fa; }}
header {{ background: #111827; color: white; padding: 16px 24px; }}
nav {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 12px; }}
nav a {{ color: #d8e2ff; text-decoration: none; font-size: 14px; }}
.primary-link {{ display: inline-block; background: #111827; color: #ffffff; padding: 9px 12px; border-radius: 6px; text-decoration: none; font-weight: 700; }}
.production-callout {{ border: 1px solid #93c5fd; border-radius: 6px; padding: 16px; margin: 16px 0; background: #eff6ff; }}
.boundary {{ display: block; color: #c7d2fe; font-size: 13px; margin-top: 6px; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
section {{ background: white; border: 1px solid #d9dee7; border-radius: 6px; padding: 20px; }}
h1 {{ margin-top: 0; font-size: 26px; }}
h2 {{ border-top: 1px solid #e5e7eb; padding-top: 16px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }}
th {{ background: #f1f5f9; }}
.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
.metrics div {{ border: 1px solid #d9dee7; border-radius: 6px; padding: 14px; }}
.metrics strong {{ display: block; font-size: 24px; }}
pre {{ white-space: pre-wrap; background: #f8fafc; padding: 12px; border: 1px solid #e5e7eb; }}
</style>
</head>
<body>
<header><strong>Dawnstrike Titan Command Center</strong><span class="boundary">Research-only; no live execution.</span><nav>{nav_html}</nav></header>
<main>{body}</main>
</body>
</html>
"""


def _markdownish(text: str) -> str:
    lines = []
    in_list = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if in_list:
                lines.append("</ul>")
                in_list = False
            continue
        if line.startswith("# "):
            if in_list:
                lines.append("</ul>")
                in_list = False
            lines.append(f"<h1>{_esc(line[2:])}</h1>")
        elif line.startswith("## "):
            if in_list:
                lines.append("</ul>")
                in_list = False
            lines.append(f"<h2>{_esc(line[3:])}</h2>")
        elif line.startswith("- "):
            if not in_list:
                lines.append("<ul>")
                in_list = True
            lines.append(f"<li>{_esc(line[2:])}</li>")
        else:
            if in_list:
                lines.append("</ul>")
                in_list = False
            lines.append(f"<p>{_esc(line)}</p>")
    if in_list:
        lines.append("</ul>")
    return "\n".join(lines)


def _table(rows: Sequence[object]) -> str:
    dict_rows = [row for row in rows if isinstance(row, dict)]
    if not dict_rows:
        return "<p>No rows available.</p>"
    fields = sorted({key for row in dict_rows for key in row})[:18]
    header = "".join(f"<th>{_esc(str(field))}</th>" for field in fields)
    body = []
    for row in dict_rows[:100]:
        body.append(
            "<tr>"
            + "".join(f"<td>{_esc(_cell(row.get(field)))}</td>" for field in fields)
            + "</tr>"
        )
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _list_html(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in items) + "</ul>"


def _write_command_center_qa(*, output_root: Path, pages: tuple[Path, ...]) -> dict[str, object]:
    page_names = tuple(sorted(path.name for path in pages))
    required_present = all(name in page_names for name in REQUIRED_PAGES)
    warnings: list[str] = []
    if not required_present:
        missing = sorted(set(REQUIRED_PAGES) - set(page_names))
        warnings.append("missing required pages: " + ", ".join(missing))
    links_checked = 0
    script_pages: list[str] = []
    absolute_path_pages: list[str] = []
    missing_banner_pages: list[str] = []
    broken_links: list[str] = []
    for path in pages:
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        if "<script" in lower:
            script_pages.append(path.name)
        if re.search(r"\b[A-Za-z]:[\\/](?![\\/])[^\"'<>\s]+", text):
            absolute_path_pages.append(path.name)
        if "research-only; no live execution." not in lower:
            missing_banner_pages.append(path.name)
        hrefs = _hrefs(text)
        links_checked += len(hrefs)
        for href in hrefs:
            if href.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if not (output_root / href).exists():
                broken_links.append(f"{path.name}->{href}")
    if script_pages:
        warnings.append("script tags found: " + ", ".join(sorted(script_pages)))
    if absolute_path_pages:
        warnings.append("absolute local paths found: " + ", ".join(sorted(absolute_path_pages)))
    if missing_banner_pages:
        warnings.append("missing research-only banner: " + ", ".join(sorted(missing_banner_pages)))
    if broken_links:
        warnings.append("broken links: " + ", ".join(sorted(broken_links)))
    payload: dict[str, object] = {
        "absolute_local_paths_clear": not absolute_path_pages,
        "broken_links": broken_links,
        "links_checked": links_checked,
        "page_count": len(pages),
        "required_page_count": len(REQUIRED_PAGES),
        "required_pages_present": required_present,
        "research_only_banner_all_pages": not missing_banner_pages,
        "schema_version": "v2.command_center_qa.v1",
        "script_tags_clear": not script_pages,
        "status": "passed" if not warnings else "passed_with_warnings",
        "warnings": warnings,
    }
    _write_json(output_root / "command_center_qa.json", payload)
    _write_qa_markdown(output_root / "command_center_qa.md", payload)
    return payload


def _write_qa_markdown(path: Path, payload: dict[str, object]) -> None:
    lines = [
        "# Command Center QA",
        "",
        f"- Status: `{payload['status']}`",
        f"- Pages: `{payload['page_count']} / {payload['required_page_count']}`",
        f"- Links checked: `{payload['links_checked']}`",
        f"- Required pages present: `{payload['required_pages_present']}`",
        f"- Script tags clear: `{payload['script_tags_clear']}`",
        f"- Absolute local paths clear: `{payload['absolute_local_paths_clear']}`",
        f"- Research-only banner all pages: `{payload['research_only_banner_all_pages']}`",
        "",
        "## Warnings",
        "",
    ]
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


def _hrefs(text: str) -> list[str]:
    parser = _HrefParser()
    parser.feed(text)
    return parser.hrefs


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _latest_file(root: Path, pattern: str) -> Path:
    if not root.exists():
        return root / "__missing__"
    matches = sorted(root.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else root / "__missing__"


def _latest_frozen_rows(root: Path) -> list[dict[str, object]]:
    latest = _latest_file(root, "*_picks*.json")
    payload = _read_json(latest, {})
    if not isinstance(payload, dict):
        return []
    rows: list[dict[str, object]] = []
    for key in (
        "accepted_candidates",
        "blocked_candidates",
        "watchlist_candidates",
        "near_setup_candidates",
        "no_setup_explanations",
        "candidates",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    return rows


def _list_any(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _write_json(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_text(path: Path) -> str:
    if not path.exists():
        return "Artifact not generated yet."
    return path.read_text(encoding="utf-8")


def _cell(value: object) -> str:
    if isinstance(value, list | tuple):
        return " | ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return "" if value is None else str(value)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)
