"""Read-only artifact adapters for Command Center X.

The adapters intentionally read local JSON/CSV/Markdown artifacts only. They do
not recompute signals, send Telegram messages, touch SQLite, or call providers.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def build_view_models(*, repo_root: Path = Path(".")) -> dict[str, dict[str, Any]]:
    """Build normalized UI view models from existing local artifacts."""
    return {
        "system_health": system_health_view(repo_root=repo_root),
        "today": today_view(repo_root=repo_root),
        "evidence": evidence_view(repo_root=repo_root),
        "paper_trading": paper_trading_view(repo_root=repo_root),
        "strategies": strategy_view(repo_root=repo_root),
        "learning": learning_view(repo_root=repo_root),
        "market_masters": market_masters_view(repo_root=repo_root),
        "automation": automation_view(repo_root=repo_root),
        "repo_inventory": repo_inventory_view(repo_root=repo_root),
    }


def system_health_view(*, repo_root: Path = Path(".")) -> dict[str, Any]:
    sentinel = _read_json(repo_root / "data/v2_omega_sentinel/status/latest_status.json", {})
    scheduler = _read_json(repo_root / "data/v2_scheduler/status/latest_status.json", {})
    autonomous = _read_json(repo_root / "data/v2_autonomous_runner/status/latest_status.json", {})
    watchdog = _read_json(repo_root / "data/v2_autonomous_runner/health/watchdog_latest.json", {})
    qa = _read_json(repo_root / "data/v2_command_center/command_center_qa.json", {})
    provider = _read_json(repo_root / "data/v2_autodata/reports/provider_readiness.json", {})
    alert = _read_json(repo_root / "data/v2_omega_sentinel/alerts/latest_alert.json", {})
    warnings = _merge_warnings(sentinel, scheduler, autonomous, watchdog, provider, alert)
    live_disabled = autonomous.get("live_trading_enabled") is False or not bool(
        autonomous.get("live_trading_enabled")
    )
    return {
        "title": "System Health",
        "status": _worst_status(
            [
                sentinel.get("status"),
                scheduler.get("status"),
                watchdog.get("status"),
                qa.get("status"),
            ],
            warnings=warnings,
        ),
        "sentinel_status": sentinel.get("status", "missing"),
        "scheduler_status": scheduler.get("status", "missing"),
        "watchdog_status": watchdog.get("status", "missing"),
        "command_center_qa_status": qa.get("status", "missing"),
        "provider_readiness": provider.get("status")
        or provider.get("readiness_status")
        or provider.get("provider_readiness_status")
        or "missing",
        "live_trading_disabled": live_disabled,
        "latest_alert_level": alert.get("level") or alert.get("status") or "missing",
        "warnings": warnings,
        "source_artifacts": _artifact_refs(
            [
                "data/v2_omega_sentinel/status/latest_status.json",
                "data/v2_scheduler/status/latest_status.json",
                "data/v2_autonomous_runner/status/latest_status.json",
                "data/v2_autonomous_runner/health/watchdog_latest.json",
                "data/v2_command_center/command_center_qa.json",
                "data/v2_autodata/reports/provider_readiness.json",
            ],
            repo_root=repo_root,
        ),
    }


def today_view(*, repo_root: Path = Path(".")) -> dict[str, Any]:
    sentinel = _read_json(repo_root / "data/v2_omega_sentinel/status/latest_status.json", {})
    scheduler = _read_json(repo_root / "data/v2_scheduler/status/latest_status.json", {})
    frozen = _read_json(
        _latest_file(repo_root / "data/v2_forward_evidence/frozen_picks", "*.json"), {}
    )
    paper_pending = _read_json(repo_root / "data/v2_paper_ops/state/pending_orders.json", {})
    paper_open = _read_json(repo_root / "data/v2_paper_ops/state/open_positions.json", {})
    learning = _read_json(
        _latest_file(repo_root / "data/v2_learning_foundry/lessons", "*.json"), {}
    )
    market_masters = _read_json(repo_root / "data/v2_market_masters/reports/report_latest.json", {})
    telegram = _read_json(repo_root / "data/v2_telegram_intel/messages/latest_message.json", {})
    accepted = _list_from(frozen, "accepted_candidates")
    blocked = _list_from(frozen, "blocked_candidates")
    watch = _list_from(frozen, "watchlist_candidates")
    near = _list_from(frozen, "near_setup_candidates")
    no_setup = _list_from(frozen, "no_setup_explanations")
    no_pick_reasons = _no_pick_reasons(telegram)
    warnings = _merge_warnings(sentinel, scheduler, frozen, learning, market_masters, telegram)
    return {
        "title": "Today",
        "status": "warning" if warnings else "ok",
        "run_date": scheduler.get("run_date") or sentinel.get("run_date") or "unknown",
        "sentinel_status": sentinel.get("status", "missing"),
        "scheduler_status": scheduler.get("status", "missing"),
        "accepted_count": len(accepted),
        "blocked_count": len(blocked),
        "watch_count": len(watch),
        "near_setup_count": len(near),
        "no_setup_count": len(no_setup),
        "pending_paper_count": _row_count(paper_pending),
        "open_paper_count": _row_count(paper_open),
        "learning_summary": learning.get("lesson")
        or learning.get("summary")
        or learning.get("status")
        or "No latest lesson summary found.",
        "market_masters_summary": {
            "status": market_masters.get("final_status") or market_masters.get("status", "missing"),
            "build_id": market_masters.get("build_id", "missing"),
            "source_count": market_masters.get("source_count", 0),
            "methodology_count": market_masters.get("methodology_count", 0),
            "primitive_count": market_masters.get("primitive_count", 0),
            "challenger_count": market_masters.get("challenger_count", 0),
            "promotion_result": market_masters.get("promotion_result", "missing"),
            "validation_triggered": market_masters.get("validation_triggered", False),
        },
        "no_pick_reasons": no_pick_reasons,
        "next_action": _next_action(warnings=warnings, accepted_count=len(accepted)),
        "warnings": warnings,
        "source_artifacts": _artifact_refs(
            [
                "data/v2_omega_sentinel/status/latest_status.json",
                "data/v2_scheduler/status/latest_status.json",
                "data/v2_forward_evidence/frozen_picks",
                "data/v2_paper_ops/state/pending_orders.json",
                "data/v2_paper_ops/state/open_positions.json",
                "data/v2_learning_foundry/lessons",
                "data/v2_market_masters/reports/report_latest.json",
                "data/v2_telegram_intel/messages/latest_message.json",
            ],
            repo_root=repo_root,
        ),
    }


def evidence_view(*, repo_root: Path = Path(".")) -> dict[str, Any]:
    data_truth = _read_json(repo_root / "data/v2_data_truth/reports/data_truth_summary.json", {})
    autodata = _read_json(repo_root / "data/v2_autodata/reports/autodata_summary.json", {})
    provider = _read_json(repo_root / "data/v2_autodata/reports/provider_readiness.json", {})
    fill_truth = _read_json(repo_root / "data/v2_fill_truth/reports/filltruth_summary.json", {})
    commit = _read_json(
        repo_root / "data/v2_evidence_commit/reports/evidence_commit_summary.json", {}
    )
    reconciliation = _read_json(
        repo_root / "data/v2_real_intraday/reports/intraday_daily_reconciliation_latest.json",
        {},
    )
    warnings = _merge_warnings(data_truth, autodata, provider, fill_truth, commit, reconciliation)
    return {
        "title": "Evidence Quality",
        "status": "warning" if warnings else "ok",
        "data_truth_status": data_truth.get("status", "missing"),
        "autodata_status": autodata.get("status", "missing"),
        "provider_status": provider.get("status")
        or provider.get("readiness_status")
        or provider.get("provider_readiness_status")
        or "missing",
        "canonical_provider": autodata.get("canonical_provider_id")
        or autodata.get("canonical_provider")
        or "unknown",
        "duplicate_timestamp_count": autodata.get("canonical_duplicate_timestamp_count", "unknown"),
        "fill_truth_status": fill_truth.get("status", "missing"),
        "commitbridge_status": commit.get("status", "missing"),
        "commitbridge_quality_score": commit.get("quality_score", "missing"),
        "reconciliation_status": reconciliation.get("status", "missing"),
        "warnings": warnings,
        "source_artifacts": _artifact_refs(
            [
                "data/v2_data_truth/reports/data_truth_summary.json",
                "data/v2_autodata/reports/autodata_summary.json",
                "data/v2_autodata/reports/provider_readiness.json",
                "data/v2_fill_truth/reports/filltruth_summary.json",
                "data/v2_evidence_commit/reports/evidence_commit_summary.json",
                "data/v2_real_intraday/reports/intraday_daily_reconciliation_latest.json",
            ],
            repo_root=repo_root,
        ),
    }


def paper_trading_view(*, repo_root: Path = Path(".")) -> dict[str, Any]:
    pending = _read_json(repo_root / "data/v2_paper_ops/state/pending_orders.json", {})
    open_positions = _read_json(repo_root / "data/v2_paper_ops/state/open_positions.json", {})
    closed = _read_json(repo_root / "data/v2_paper_ops/state/closed_trades.json", {})
    calendar_returns = _read_csv(
        repo_root / "data/v2_paper_ops/calendar/strategy_daily_returns.csv"
    )
    equity = _read_csv(repo_root / "data/v2_paper_ops/calendar/strategy_equity_curves.csv")
    drawdowns = _read_csv(repo_root / "data/v2_paper_ops/calendar/strategy_drawdowns.csv")
    commit_events = _read_json(
        repo_root / "data/v2_evidence_commit/reports/evidence_commit_summary.json",
        {},
    )
    warnings = _merge_warnings(pending, open_positions, closed, commit_events)
    return {
        "title": "Paper Positions",
        "status": "warning" if warnings else "ok",
        "pending_orders": _rows_from_payload(pending),
        "open_positions": _rows_from_payload(open_positions),
        "closed_trades": _rows_from_payload(closed),
        "calendar_returns": calendar_returns[:250],
        "equity_curves": equity[:250],
        "drawdowns": drawdowns[:250],
        "commitbridge_events": commit_events.get("commit_events", "missing"),
        "warnings": warnings,
        "source_artifacts": _artifact_refs(
            [
                "data/v2_paper_ops/state/pending_orders.json",
                "data/v2_paper_ops/state/open_positions.json",
                "data/v2_paper_ops/calendar/strategy_daily_returns.csv",
                "data/v2_paper_ops/calendar/strategy_equity_curves.csv",
                "data/v2_paper_ops/calendar/strategy_drawdowns.csv",
                "data/v2_evidence_commit/reports/evidence_commit_summary.json",
            ],
            repo_root=repo_root,
        ),
    }


def strategy_view(*, repo_root: Path = Path(".")) -> dict[str, Any]:
    evidence = _read_json(
        repo_root / "data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json",
        {},
    )
    calendar = _read_csv(repo_root / "data/v2_paper_ops/calendar/strategy_daily_returns.csv")
    drawdowns = _read_csv(repo_root / "data/v2_paper_ops/calendar/strategy_drawdowns.csv")
    learning_challengers = _read_json(
        repo_root / "data/v2_learning_foundry/candidates/challenger_registry.json",
        {},
    )
    market_challengers = _read_json(
        repo_root / "data/v2_market_masters/candidates/challenger_registry.json",
        {},
    )
    rows = _strategy_rows(evidence, learning_challengers, market_challengers, drawdowns)
    warnings = _merge_warnings(evidence, learning_challengers, market_challengers)
    return {
        "title": "Strategies",
        "status": "warning" if warnings else "ok",
        "validated_strategy_count": sum(1 for row in rows if row.get("validated") is True),
        "strategies": rows,
        "calendar_sample": calendar[:120],
        "drawdown_sample": drawdowns[:120],
        "warnings": warnings
        + (
            []
            if any(row.get("validated") is True for row in rows)
            else ["No strategy is validated yet."]
        ),
        "source_artifacts": _artifact_refs(
            [
                "data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json",
                "data/v2_paper_ops/calendar/strategy_daily_returns.csv",
                "data/v2_paper_ops/calendar/strategy_drawdowns.csv",
                "data/v2_learning_foundry/candidates/challenger_registry.json",
                "data/v2_market_masters/candidates/challenger_registry.json",
            ],
            repo_root=repo_root,
        ),
    }


def learning_view(*, repo_root: Path = Path(".")) -> dict[str, Any]:
    summary = _read_json(
        repo_root / "data/v2_learning_foundry/reports/learning_foundry_summary.json", {}
    )
    verify = _read_json(repo_root / "data/v2_learning_foundry/reports/verify_latest.json", {})
    lesson = _read_json(_latest_file(repo_root / "data/v2_learning_foundry/lessons", "*.json"), {})
    promotion = _read_json(repo_root / "data/v2_learning_foundry/reports/promotion_review.json", {})
    sync = _read_json(repo_root / "data/v2_learning_foundry/reports/market_masters_sync.json", {})
    warnings = _merge_warnings(summary, verify, lesson, promotion, sync)
    return {
        "title": "What Dawnstrike Learned",
        "status": "warning" if warnings else "ok",
        "verify_status": verify.get("status", "missing"),
        "feature_count": summary.get("features") or summary.get("feature_count") or "missing",
        "label_count": summary.get("labels") or summary.get("label_count") or "missing",
        "regime": summary.get("regime") or lesson.get("regime") or "missing",
        "news_status": summary.get("news") or "missing",
        "candidate_count": summary.get("candidates") or "missing",
        "shadow_count": summary.get("shadow") or "missing",
        "promotion_status": promotion.get("status") or summary.get("promotion") or "missing",
        "daily_lesson": lesson,
        "market_masters_sync_status": sync.get("status", "missing"),
        "warnings": warnings,
        "source_artifacts": _artifact_refs(
            [
                "data/v2_learning_foundry/reports/learning_foundry_summary.json",
                "data/v2_learning_foundry/reports/verify_latest.json",
                "data/v2_learning_foundry/lessons",
                "data/v2_learning_foundry/reports/promotion_review.json",
                "data/v2_learning_foundry/reports/market_masters_sync.json",
            ],
            repo_root=repo_root,
        ),
    }


def market_masters_view(*, repo_root: Path = Path(".")) -> dict[str, Any]:
    report = _read_json(repo_root / "data/v2_market_masters/reports/report_latest.json", {})
    verify = _read_json(repo_root / "data/v2_market_masters/reports/verify_latest.json", {})
    sources = _read_json(repo_root / "data/v2_market_masters/research/source_register.json", {})
    methods = _read_json(
        repo_root / "data/v2_market_masters/research/methodology_taxonomy.json", {}
    )
    primitives = _read_json(
        repo_root / "data/v2_market_masters/primitives/strategy_primitives.json", {}
    )
    challengers = _read_json(
        repo_root / "data/v2_market_masters/candidates/challenger_registry.json", {}
    )
    shadow = _read_json(
        _latest_file(repo_root / "data/v2_market_masters/shadow_runs", "*.json"), {}
    )
    evals = _read_json(_latest_file(repo_root / "data/v2_market_masters/evals", "*_eval.json"), {})
    warnings = _merge_warnings(report, verify, shadow, evals)
    return {
        "title": "Market Masters",
        "status": verify.get("status") or report.get("status") or "missing",
        "build_id": report.get("build_id", "missing"),
        "source_count": report.get("source_count", 0),
        "methodology_count": report.get("methodology_count", 0),
        "primitive_count": report.get("primitive_count", 0),
        "challenger_count": report.get("challenger_count", 0),
        "promotion_result": report.get("promotion_result", "missing"),
        "validation_triggered": report.get("validation_triggered", False),
        "sources": _rows_from_payload(sources),
        "methodologies": _rows_from_payload(methods),
        "primitives": _rows_from_payload(primitives),
        "challengers": _rows_from_payload(challengers),
        "shadow_results": _rows_from_payload(shadow),
        "evals": _rows_from_payload(evals),
        "warnings": warnings
        + (
            ["No Market Masters challenger is allowed to replace a champion from this UI."]
            if report
            else ["Market Masters report is missing."]
        ),
        "source_artifacts": _artifact_refs(
            [
                "data/v2_market_masters/reports/report_latest.json",
                "data/v2_market_masters/reports/verify_latest.json",
                "data/v2_market_masters/research/source_register.json",
                "data/v2_market_masters/research/methodology_taxonomy.json",
                "data/v2_market_masters/primitives/strategy_primitives.json",
                "data/v2_market_masters/candidates/challenger_registry.json",
                "data/v2_market_masters/shadow_runs",
                "data/v2_market_masters/evals",
            ],
            repo_root=repo_root,
        ),
    }


def automation_view(*, repo_root: Path = Path(".")) -> dict[str, Any]:
    autonomous = _read_json(repo_root / "data/v2_autonomous_runner/status/latest_status.json", {})
    tasks = _read_json(
        repo_root / "data/v2_autonomous_runner/task_definitions/omega_autonomous_tasks.json",
        {},
    )
    watchdog = _read_json(repo_root / "data/v2_autonomous_runner/health/watchdog_latest.json", {})
    scheduler = _read_json(repo_root / "data/v2_scheduler/status/latest_status.json", {})
    telegram = _read_json(repo_root / "data/v2_telegram_intel/reports/verify_latest.json", {})
    readiness = _read_json(repo_root / "data/v2_telegram_intel/reports/readiness_latest.json", {})
    warnings = _merge_warnings(autonomous, tasks, watchdog, scheduler, telegram, readiness)
    return {
        "title": "Automation",
        "status": "warning" if warnings else "ok",
        "tasks_installed": autonomous.get("installed_task_count")
        or watchdog.get("tasks_installed")
        or _row_count(tasks),
        "task_count": autonomous.get("task_count", "missing"),
        "tasks": autonomous.get("tasks") if isinstance(autonomous.get("tasks"), list) else [],
        "missed_runs": autonomous.get("missed_runs", {}),
        "scheduler_status": scheduler.get("status", "missing"),
        "watchdog_status": watchdog.get("status", "missing"),
        "telegram_verify_status": telegram.get("status", "missing"),
        "telegram_readiness": readiness.get("readiness_status")
        or telegram.get("readiness_status")
        or "missing",
        "external_alerts_enabled": autonomous.get("external_alerts_enabled", False),
        "live_trading_disabled": not bool(autonomous.get("live_trading_enabled")),
        "warnings": warnings,
        "source_artifacts": _artifact_refs(
            [
                "data/v2_autonomous_runner/status/latest_status.json",
                "data/v2_autonomous_runner/task_definitions/omega_autonomous_tasks.json",
                "data/v2_autonomous_runner/health/watchdog_latest.json",
                "data/v2_scheduler/status/latest_status.json",
                "data/v2_telegram_intel/reports/verify_latest.json",
            ],
            repo_root=repo_root,
        ),
    }


def repo_inventory_view(*, repo_root: Path = Path(".")) -> dict[str, Any]:
    inventory = _read_json(
        repo_root / "data/v2_command_center_x/reports/repo_inventory.json",
        {},
    )
    return {
        "title": "System Map",
        "status": "ok" if inventory else "missing",
        "inventory": inventory,
        "warnings": [] if inventory else ["Command Center X inventory has not been generated."],
        "source_artifacts": _artifact_refs(
            ["data/v2_command_center_x/reports/repo_inventory.json"],
            repo_root=repo_root,
        ),
    }


def write_view_models(
    *, output_root: Path, repo_root: Path = Path(".")
) -> dict[str, dict[str, Any]]:
    views = build_view_models(repo_root=repo_root)
    data_dir = output_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in views.items():
        _write_json(data_dir / f"{name}.json", payload)
    return views


def _read_json(path: Path, default: Any) -> Any:
    if path.is_dir():
        return default
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.is_dir():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _latest_file(root: Path, pattern: str) -> Path:
    if root.is_file():
        return root
    if not root.exists():
        return root / "__missing__"
    matches = sorted(root.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[0] if matches else root / "__missing__"


def _list_from(payload: Any, key: str) -> list[Any]:
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return payload[key]
    return []


def _rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "rows",
        "items",
        "decisions",
        "proposals",
        "candidates",
        "challengers",
        "sources",
        "methodologies",
        "primitives",
        "results",
        "open_positions",
        "pending_orders",
        "closed_trades",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return [{"field": str(key), "value": value} for key, value in sorted(payload.items())[:80]]


def _row_count(payload: Any) -> int:
    return len(_rows_from_payload(payload))


def _merge_warnings(*payloads: Any) -> list[str]:
    warnings: list[str] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("warnings", "failures", "errors"):
            value = payload.get(key)
            if isinstance(value, list):
                warnings.extend(str(item) for item in value if str(item))
            elif value:
                warnings.append(str(value))
        status = str(payload.get("status", "")).lower()
        if status in {"missing", "failed", "critical", "blocked"}:
            warnings.append(f"Artifact status is {status}.")
    return _dedupe(warnings)[:80]


def _worst_status(statuses: list[Any], *, warnings: list[str]) -> str:
    normalized = {str(status).lower() for status in statuses if status is not None}
    if {"failed", "critical"} & normalized:
        return "critical"
    if warnings or {"blocked", "warning", "passed_with_warnings"} & normalized:
        return "warning"
    if {"missing"} & normalized:
        return "missing"
    return "ok"


def _artifact_refs(paths: list[str], *, repo_root: Path) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for raw in paths:
        path = repo_root / raw
        refs.append(
            {
                "path": raw,
                "exists": path.exists(),
                "kind": "directory" if path.is_dir() else "file",
            }
        )
    return refs


def _no_pick_reasons(payload: Any) -> list[str]:
    text = ""
    if isinstance(payload, dict):
        text = str(
            payload.get("text") or "\n".join(str(item) for item in payload.get("chunks", []))
        )
    reasons: list[str] = []
    collect = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("why no official"):
            collect = True
            continue
        if collect and line and line[0].isdigit():
            reasons.append(line)
        elif collect and line.startswith("RiskHub"):
            break
    return reasons[:8] or ["No latest no-picks explanation found in Telegram artifacts."]


def _next_action(*, warnings: list[str], accepted_count: int) -> str:
    if accepted_count:
        return (
            "Review official paper candidates and evidence; "
            "do not treat them as live-trade instructions."
        )
    if warnings:
        return "Inspect warnings, provider evidence, RiskHub blocks, and no-picks reasons."
    return "Review the latest reports and wait for the next autonomous run."


def _strategy_rows(
    evidence: Any,
    learning_challengers: Any,
    market_challengers: Any,
    drawdowns: list[dict[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    evidence_rows = _rows_from_payload(evidence)
    for row in evidence_rows[:80]:
        rows.append(
            {
                "strategy": row.get("strategy")
                or row.get("strategy_id")
                or row.get("name")
                or "unknown",
                "status": row.get("status") or row.get("gate_status") or "evidence",
                "sample_size": row.get("sample_size") or row.get("closed_trade_count") or "n/a",
                "win_rate": row.get("win_rate") or row.get("win_rate_pct") or "n/a",
                "expectancy": row.get("expectancy") or row.get("expectancy_r") or "n/a",
                "drawdown": row.get("max_drawdown") or "n/a",
                "validated": bool(row.get("validated") is True),
                "source": "strategy_evidence",
            }
        )
    for payload, source in (
        (learning_challengers, "learning_foundry"),
        (market_challengers, "market_masters"),
    ):
        for row in _rows_from_payload(payload)[:80]:
            rows.append(
                {
                    "strategy": row.get("id")
                    or row.get("strategy_id")
                    or row.get("name")
                    or row.get("challenger_id")
                    or "unknown",
                    "status": row.get("status") or row.get("promotion_status") or "shadow",
                    "sample_size": row.get("sample_size") or "n/a",
                    "win_rate": row.get("win_rate") or "n/a",
                    "expectancy": row.get("expectancy") or "n/a",
                    "drawdown": row.get("max_drawdown") or "n/a",
                    "validated": False,
                    "source": source,
                }
            )
    for row in drawdowns[:40]:
        rows.append(
            {
                "strategy": row.get("strategy") or row.get("strategy_id") or "unknown",
                "status": "paper-calendar",
                "sample_size": "n/a",
                "win_rate": "n/a",
                "expectancy": "n/a",
                "drawdown": row.get("drawdown") or row.get("max_drawdown") or "n/a",
                "validated": False,
                "source": "paper_ops_calendar",
            }
        )
    return rows[:180]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped
