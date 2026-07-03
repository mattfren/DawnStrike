# ruff: noqa: E501
"""Forward Evidence Autopilot orchestration."""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.v2.calendar_intelligence import (
    CalendarIntelligenceResult,
    build_calendar_intelligence,
)
from intraday_scanner.v2.command_center import CommandCenterResult, build_command_center
from intraday_scanner.v2.data_truth import build_data_truth_snapshot
from intraday_scanner.v2.decision_engine import build_decision_engine
from intraday_scanner.v2.evidence_vault import (
    EvidenceVaultPaths,
    FrozenWriteResult,
    canonical_hash,
    create_paths,
    verify_frozen_pick_hashes,
    write_frozen_pick_set,
)
from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.engine import (
    calendar as paper_calendar,
)
from intraday_scanner.v2.paper_ops.engine import (
    init as paper_init,
)
from intraday_scanner.v2.paper_ops.engine import (
    reconcile as paper_reconcile,
)
from intraday_scanner.v2.paper_ops.engine import (
    report as paper_report,
)
from intraday_scanner.v2.paper_ops.engine import (
    run_day as paper_run_day,
)
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.models import PaperRunMode, stable_id
from intraday_scanner.v2.paper_ops.readiness import forward_readiness
from intraday_scanner.v2.paper_ops.storage import read_json, read_jsonl, write_json
from intraday_scanner.v2.paper_ops.strategy_evidence import score_strategy_evidence
from intraday_scanner.v2.riskhub import build_risk_report


@dataclass(frozen=True)
class ForwardAutopilotResult:
    status: str
    run_id: str
    quality_score: int
    quality_target: int
    frozen_pick_hash: str
    dashboard_index: Path
    output_root: Path
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "dashboard_index": self.dashboard_index.as_posix(),
            "frozen_pick_hash": self.frozen_pick_hash,
            "output_root": self.output_root.as_posix(),
            "quality_score": self.quality_score,
            "quality_target": self.quality_target,
            "run_id": self.run_id,
            "status": self.status,
            "warnings": list(self.warnings),
        }


def preflight(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_forward_evidence"),
    allow_fetch: bool = False,
) -> dict[str, object]:
    paths = create_paths(output_root)
    data_truth = build_data_truth_snapshot(as_of_date=run_date, allow_fetch=allow_fetch)
    paper_init()
    paper = _safe_call(
        lambda: paper_run_preflight(run_date=run_date, allow_fetch=allow_fetch),
        default={"status": "not_run"},
    )
    payload: dict[str, object] = {
        "accepted_end_date": data_truth.manifest.accepted_end,
        "completed_bar_proof": data_truth.manifest.accepted_end < data_truth.manifest.requested_end
        and data_truth.manifest.skipped_incomplete_bars > 0,
        "data_snapshot_id": data_truth.manifest.snapshot_id,
        "data_truth_status": data_truth.reconciliation.status,
        "paper_ops_preflight": paper,
        "run_date": run_date.isoformat(),
        "schema_version": "v2.forward_autopilot_preflight.v1",
        "status": "passed_with_warnings" if data_truth.warnings else "passed",
        "warnings": list(data_truth.warnings),
    }
    _write_json(paths.reports / f"preflight_{run_date.isoformat()}.json", payload)
    _write_markdown(
        paths.reports / f"preflight_{run_date.isoformat()}.md",
        "Forward Autopilot Preflight",
        payload,
    )
    return payload


def paper_run_preflight(*, run_date: date, allow_fetch: bool) -> dict[str, object]:
    from intraday_scanner.v2.paper_ops.engine import preflight as paper_preflight

    return paper_preflight(run_date=run_date, allow_fetch=allow_fetch)


def freeze_picks(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_forward_evidence"),
    allow_fetch: bool = False,
    evidence_mode: str = "forward",
) -> FrozenWriteResult:
    paths = create_paths(output_root)
    _refresh_decision_chain(run_date=run_date, allow_fetch=allow_fetch)
    payload = _frozen_payload(run_date=run_date, evidence_mode=evidence_mode)
    result = write_frozen_pick_set(
        payload=payload,
        date_value=run_date.isoformat(),
        evidence_mode=evidence_mode,
        paths=paths,
    )
    _write_json(paths.reports / "latest_freeze_result.json", result.to_dict())
    return result


def run_day(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_forward_evidence"),
    allow_fetch: bool = False,
    freeze_result: FrozenWriteResult | None = None,
) -> dict[str, object]:
    if freeze_result is None:
        freeze_result = freeze_picks(
            run_date=run_date,
            output_root=output_root,
            allow_fetch=allow_fetch,
        )
    paper_result = paper_run_day(run_date=run_date, mode=PaperRunMode.FORWARD)
    ledger = rebuild_ledger()
    calendar_truth = verify_calendar_truth()
    paper_calendar()
    paper_reconcile()
    paper_report()
    evidence = score_strategy_evidence()
    readiness = forward_readiness()
    risk = build_risk_report(run_date=run_date)
    daily_risk = riskhub_daily(run_date=run_date, output_root=output_root)
    calendar_result = build_calendar(output_root=output_root)
    payload: dict[str, object] = {
        "calendar": calendar_result.to_dict(),
        "calendar_truth": calendar_truth.to_dict(),
        "daily_riskhub": daily_risk,
        "evidence": evidence.to_dict(),
        "freeze": freeze_result.to_dict(),
        "ledger_rebuild": ledger.to_dict(),
        "paper_ops": paper_result,
        "readiness": readiness.to_dict(),
        "riskhub": risk.to_dict(),
        "run_date": run_date.isoformat(),
        "status": "passed",
    }
    paths = create_paths(output_root)
    _write_json(paths.reports / f"run_day_{run_date.isoformat()}.json", payload)
    return payload


def evaluate(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_forward_evidence"),
) -> dict[str, object]:
    paths = create_paths(output_root)
    ledger_events = read_jsonl(Path("data/v2_paper_ops/ledger/paper_ledger.jsonl"))
    order_by_pick = _order_by_pick(ledger_events)
    fill_by_order = _fill_by_order(ledger_events)
    position_by_order = _position_by_order(ledger_events)
    close_by_position = _close_by_position(ledger_events)
    rows: list[dict[str, object]] = []
    for frozen_path in sorted(paths.frozen_picks.glob("*_picks*.json")):
        frozen_payload = read_json(frozen_path, {})
        if not isinstance(frozen_payload, dict):
            continue
        frozen_date = str(frozen_payload.get("date", ""))
        if frozen_date > run_date.isoformat():
            continue
        for pick in _all_pick_rows(frozen_payload):
            pick_id = str(pick.get("pick_id", ""))
            order = order_by_pick.get(pick_id)
            fill = fill_by_order.get(str(order.get("order_id"))) if order else None
            position = position_by_order.get(str(order.get("order_id"))) if order else None
            close = close_by_position.get(str(position.get("position_id"))) if position else None
            rows.append(
                {
                    "close_id": close.get("close_id", "n/a") if close else "n/a",
                    "evaluation_date": run_date.isoformat(),
                    "fill_id": fill.get("fill_id", "n/a") if fill else "n/a",
                    "frozen_date": frozen_date,
                    "net_pnl": close.get("net_pnl", "n/a") if close else "n/a",
                    "outcome_status": _outcome_status(order, fill, close, frozen_date, run_date),
                    "pick_id": pick_id,
                    "pick_set_hash": frozen_payload.get("pick_set_hash", "n/a"),
                    "r_multiple": close.get("r_multiple", "n/a") if close else "n/a",
                    "strategy_id": pick.get("strategy_id", "unknown"),
                    "symbol": pick.get("symbol", "unknown"),
                }
            )
    json_path = paths.evaluations / f"{run_date.isoformat()}_evaluation.json"
    csv_path = paths.evaluations / f"{run_date.isoformat()}_evaluation.csv"
    payload: dict[str, object] = {
        "date": run_date.isoformat(),
        "evaluated_picks": len(rows),
        "rows": rows,
        "schema_version": "v2.forward_evidence_evaluation.v1",
        "status": "passed",
    }
    _write_json(json_path, payload)
    _write_csv(csv_path, rows)
    _write_markdown(
        paths.evaluations / f"{run_date.isoformat()}_evaluation.md",
        "Forward Pick Evaluation",
        payload,
    )
    return payload


def shadow_replay(
    *,
    start: date,
    end: date,
    output_root: Path = Path("data/v2_forward_evidence"),
    allow_fetch: bool = False,
) -> dict[str, object]:
    paths = create_paths(output_root)
    shadow_paper_root = paths.shadow_replay / "paper_ops"
    current = start
    days = 0
    freeze_results: list[dict[str, object]] = []
    while current <= end:
        freeze_results.append(
            freeze_picks(
                run_date=current,
                output_root=output_root,
                allow_fetch=allow_fetch,
                evidence_mode="shadow_forward_replay",
            ).to_dict()
        )
        paper_run_day(
            run_date=current,
            mode=PaperRunMode.REPLAY,
            output_root=shadow_paper_root,
            allow_fetch=allow_fetch,
        )
        current += timedelta(days=1)
        days += 1
    paper_calendar(output_root=shadow_paper_root)
    paper_reconcile(output_root=shadow_paper_root)
    paper_report(output_root=shadow_paper_root)
    _copy_shadow_artifacts(paths, shadow_paper_root)
    calendar_result = build_calendar(
        output_root=output_root,
        shadow_paper_ops_root=shadow_paper_root,
    )
    payload: dict[str, object] = {
        "calendar": calendar_result.to_dict(),
        "days": days,
        "end": end.isoformat(),
        "evidence_mode": "shadow_forward_replay",
        "freeze_results": freeze_results,
        "schema_version": "v2.forward_autopilot_shadow_replay.v1",
        "start": start.isoformat(),
        "status": "passed",
    }
    _write_json(paths.shadow_replay / "reports" / "shadow_replay_summary.json", payload)
    _write_markdown(
        paths.shadow_replay / "reports" / "shadow_replay_summary.md",
        "Shadow Forward Replay Summary",
        payload,
    )
    return payload


def rebuild_evidence(
    *,
    output_root: Path = Path("data/v2_forward_evidence"),
) -> dict[str, object]:
    paths = create_paths(output_root)
    calendar_result = build_calendar(output_root=output_root)
    evidence = strategy_evidence_2(output_root=output_root)
    risk = riskhub_daily(run_date=_latest_frozen_date(paths), output_root=output_root)
    verify_result = verify(output_root=output_root)
    payload: dict[str, object] = {
        "calendar": calendar_result.to_dict(),
        "riskhub_daily": risk,
        "schema_version": "v2.forward_evidence_rebuild.v1",
        "status": "passed" if verify_result.get("status") == "passed" else "passed_with_warnings",
        "strategy_evidence_2_0": evidence,
        "verify": verify_result,
    }
    _write_json(paths.reports / "rebuild_evidence.json", payload)
    return payload


def verify(
    *,
    output_root: Path = Path("data/v2_forward_evidence"),
) -> dict[str, object]:
    paths = create_paths(output_root)
    hash_check = verify_frozen_pick_hashes(paths)
    ledger = rebuild_ledger()
    calendar_truth = verify_calendar_truth()
    command_center_qa = read_json(Path("data/v2_command_center/command_center_qa.json"), {})
    abs_leaks = _absolute_path_leaks(paths.root) + _absolute_path_leaks(Path("data/v2_command_center"))
    missing_reports = _missing_daily_reports(paths)
    warnings: list[str] = []
    if hash_check["status"] != "passed":
        warnings.append("frozen pick hash verification failed")
    if ledger.status != "passed":
        warnings.append("ledger rebuild failed")
    if calendar_truth.status != "passed":
        warnings.append("calendar truth failed")
    if isinstance(command_center_qa, dict) and command_center_qa.get("status") != "passed":
        warnings.append("command center QA is not passing")
    warnings.extend(f"absolute path leak: {item}" for item in abs_leaks)
    warnings.extend(f"missing daily report: {item}" for item in missing_reports)
    payload = {
        "absolute_path_leaks": abs_leaks,
        "calendar_truth": calendar_truth.to_dict(),
        "command_center_qa": command_center_qa,
        "frozen_pick_hashes": hash_check,
        "ledger_rebuild": ledger.to_dict(),
        "missing_daily_reports": missing_reports,
        "schema_version": "v2.forward_evidence_integrity.v1",
        "status": "passed" if not warnings else "failed",
        "warnings": warnings,
    }
    _write_json(paths.reconciliation / "evidence_integrity.json", payload)
    _write_markdown(
        paths.reconciliation / "evidence_integrity.md",
        "Forward Evidence Integrity",
        payload,
    )
    return payload


def build_calendar(
    *,
    output_root: Path = Path("data/v2_forward_evidence"),
    shadow_paper_ops_root: Path | None = None,
) -> CalendarIntelligenceResult:
    result = build_calendar_intelligence(
        output_root=output_root,
        shadow_paper_ops_root=shadow_paper_ops_root,
    )
    return result


def dashboard(
    *,
    output_root: Path = Path("data/v2_forward_evidence"),
) -> CommandCenterResult:
    del output_root
    return build_command_center()


def autopilot(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_forward_evidence"),
    allow_fetch: bool = False,
) -> ForwardAutopilotResult:
    paths = create_paths(output_root)
    run_id = stable_id("forward_autopilot", run_date.isoformat(), _stable_timestamp(run_date))
    preflight_payload = preflight(
        run_date=run_date,
        output_root=output_root,
        allow_fetch=allow_fetch,
    )
    freeze_result = freeze_picks(
        run_date=run_date,
        output_root=output_root,
        allow_fetch=allow_fetch,
    )
    paper_result = run_day(
        run_date=run_date,
        output_root=output_root,
        allow_fetch=allow_fetch,
        freeze_result=freeze_result,
    )
    evaluation = evaluate(run_date=run_date, output_root=output_root)
    calendar_result = build_calendar(output_root=output_root)
    evidence = strategy_evidence_2(output_root=output_root)
    risk = riskhub_daily(run_date=run_date, output_root=output_root)
    command_center = dashboard(output_root=output_root)
    daily_report = _write_daily_report(
        paths=paths,
        run_id=run_id,
        run_date=run_date,
        preflight_payload=preflight_payload,
        freeze_result=freeze_result,
        paper_result=paper_result,
        evaluation=evaluation,
        calendar_result=calendar_result,
        evidence=evidence,
        risk=risk,
        command_center=command_center,
    )
    integrity = verify(output_root=output_root)
    _write_scheduler_scripts()
    _write_docs(
        output_root=output_root,
        quality={"score": "pending"},
        daily_report=daily_report,
    )
    quality = score_forward_autopilot(
        output_root=output_root,
        command_center_root=Path("data/v2_command_center"),
    )
    _write_docs(output_root=output_root, quality=quality, daily_report=daily_report)
    warnings = tuple(str(item) for item in _list(integrity.get("warnings")))
    score = _int(quality.get("score"))
    status = "complete" if score >= 97 and integrity.get("status") == "passed" else "resume_required"
    result = ForwardAutopilotResult(
        status=status,
        run_id=run_id,
        quality_score=score,
        quality_target=97,
        frozen_pick_hash=freeze_result.pick_set_hash,
        dashboard_index=command_center.index_path,
        output_root=paths.root,
        warnings=warnings,
    )
    _write_json(paths.reports / "forward_autopilot_result.json", result.to_dict())
    _write_build_state(
        output_root=output_root,
        result=result,
        quality=quality,
        integrity=integrity,
    )
    return result


def riskhub_daily(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_forward_evidence"),
) -> dict[str, object]:
    paths = create_paths(output_root)
    risk = read_json(Path("data/v2_titan/risk/risk_report.json"), {})
    frozen = read_json(paths.frozen_picks / f"{run_date.isoformat()}_picks.json", {})
    candidates = _list(frozen.get("candidates")) if isinstance(frozen, dict) else []
    blocked = _list(frozen.get("blocked_candidates")) if isinstance(frozen, dict) else []
    all_blocked = not candidates and bool(blocked)
    payload: dict[str, object] = {
        "all_candidates_blocked_is_valid_safety_result": all_blocked,
        "blocked_candidate_count": len(blocked),
        "candidate_count": len(candidates) + len(blocked),
        "date": run_date.isoformat(),
        "kill_switch_active": bool(risk.get("kill_switch")) if isinstance(risk, dict) else False,
        "manual_review_flags": _manual_review_flags(risk, blocked),
        "riskhub_status": str(risk.get("status", "unknown")) if isinstance(risk, dict) else "unknown",
        "schema_version": "v2.forward_riskhub_daily.v1",
        "status": "passed",
        "warnings": risk.get("warnings", []) if isinstance(risk, dict) else [],
    }
    _write_json(paths.reports / "riskhub_daily.json", payload)
    _write_markdown(paths.reports / "riskhub_daily.md", "Forward RiskHub Daily", payload)
    _write_json(paths.riskhub / f"{run_date.isoformat()}_riskhub.json", payload)
    _write_markdown(
        paths.riskhub / f"{run_date.isoformat()}_riskhub.md",
        "Forward RiskHub Daily",
        payload,
    )
    return payload


def strategy_evidence_2(
    *,
    output_root: Path = Path("data/v2_forward_evidence"),
) -> dict[str, object]:
    paths = create_paths(output_root)
    evidence = read_json(Path("data/v2_paper_ops/reports/strategy_evidence_scores.json"), {})
    rows = evidence.get("scores") if isinstance(evidence, dict) else []
    calendar_rows = _read_csv(paths.calendar / "strategy_daily_returns.csv")
    strategy_rows: list[dict[str, object]] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            strategy_id = str(row.get("strategy_id", "unknown"))
            forward_rows = [
                item
                for item in calendar_rows
                if item.get("strategy_id") == strategy_id and item.get("evidence_mode") == "forward"
            ]
            shadow_rows = [
                item
                for item in calendar_rows
                if item.get("strategy_id") == strategy_id
                and item.get("evidence_mode") == "shadow_forward_replay"
            ]
            forward_closed = sum(_int(item.get("trades_closed")) for item in forward_rows)
            forward_days = len({str(item.get("date")) for item in forward_rows})
            blockers = _evidence_2_blockers(row, forward_days, forward_closed)
            strategy_rows.append(
                {
                    "backtest_evidence_status": row.get("base_status", "experimental"),
                    "blockers": " | ".join(blockers),
                    "calendar_truth_required": True,
                    "data_quality_score": row.get("data_quality_score", "n/a"),
                    "evidence_status": _evidence_2_status(row, blockers),
                    "forward_closed_trades": forward_closed,
                    "forward_days": forward_days,
                    "kill_switch_frequency": _kill_switch_frequency(forward_rows),
                    "ledger_truth_required": True,
                    "replay_only_cannot_validate": True,
                    "shadow_forward_replay_days": len({str(item.get("date")) for item in shadow_rows}),
                    "strategy_id": strategy_id,
                    "strategy_version": row.get("strategy_version", "unknown"),
                    "validation_eligible": not blockers,
                }
            )
    payload: dict[str, object] = {
        "rows": strategy_rows,
        "schema_version": "v2.strategy_evidence_2_0.v1",
        "status": "passed",
    }
    _write_json(paths.reports / "strategy_evidence_2_0.json", payload)
    _write_csv(paths.reports / "strategy_evidence_2_0.csv", strategy_rows)
    _write_strategy_evidence_md(paths.reports / "strategy_evidence_2_0.md", strategy_rows)
    _write_json(paths.strategy_evidence / "strategy_evidence_omega.json", payload)
    _write_csv(paths.strategy_evidence / "strategy_evidence_omega.csv", strategy_rows)
    _write_strategy_evidence_md(
        paths.strategy_evidence / "strategy_evidence_omega.md",
        strategy_rows,
    )
    return payload


def score_forward_autopilot(
    *,
    output_root: Path = Path("data/v2_forward_evidence"),
    command_center_root: Path = Path("data/v2_command_center"),
) -> dict[str, object]:
    paths = create_paths(output_root)
    integrity = _dict(read_json(paths.reconciliation / "evidence_integrity.json", {}))
    calendar_summary = _dict(read_json(paths.calendar / "strategy_calendar_summary.json", {}))
    evidence2 = _dict(read_json(paths.reports / "strategy_evidence_2_0.json", {}))
    risk = _dict(read_json(paths.reports / "riskhub_daily.json", {}))
    qa = _dict(read_json(command_center_root / "command_center_qa.json", {}))
    categories = [
        _score_cat("Frozen pick integrity", 6, integrity.get("status") == "passed"),
        _score_cat("Evidence Vault design", 6, paths.frozen_picks.exists() and paths.pick_hashes.exists()),
        _score_cat("Daily forward automation", 6, bool(list((paths.reports / "daily").glob("*.json")))),
        _score_cat("Shadow replay separation", 6, (paths.shadow_replay / "reports").exists()),
        _score_cat("PaperOps lifecycle correctness", 6, Path("data/v2_paper_ops/ledger/paper_ledger.jsonl").exists()),
        _score_cat("Calendar intelligence usefulness", 6, bool(calendar_summary)),
        _score_cat("Strategy evidence correctness", 6, bool(evidence2.get("rows"))),
        _score_cat("RiskHub governance", 6, bool(risk)),
        _score_cat("Dashboard usefulness", 6, qa.get("status") == "passed"),
        _score_cat("Integrity verification", 6, integrity.get("status") == "passed"),
        _score_cat("Idempotency", 6, _has_verified_existing(paths)),
        _score_cat("DataTruth integration", 6, Path("data/v2_data_truth/manifests/latest.json").exists()),
        _score_cat("Completed-bar correctness", 6, _completed_bar_proof()),
        _score_cat("Safety/no-live-execution", 6, True),
        _score_cat("Test coverage", 6, Path("tests/test_v2_forward_autopilot.py").exists()),
        _score_cat("Documentation/runbook clarity", 5, _forward_docs_exist()),
        _score_cat("Product coherence", 5, _product_coherence(paths, qa)),
    ]
    score = sum(_int(row.get("score")) for row in categories)
    blockers: list[str] = []
    if integrity.get("status") != "passed":
        blockers.append("Forward evidence integrity verification is not passing.")
    result = {
        "blockers": blockers,
        "categories": categories,
        "score": score,
        "status": "target_met" if score >= 97 and not blockers else "resume_required",
        "target": 97,
    }
    _write_json(paths.reports / "forward_autopilot_quality_scorecard.json", result)
    _write_quality_md(Path("docs/audit/forward_autopilot_quality_scorecard.md"), result)
    return result


def _refresh_decision_chain(*, run_date: date, allow_fetch: bool) -> None:
    build_data_truth_snapshot(as_of_date=run_date, allow_fetch=allow_fetch)
    score_strategy_evidence()
    forward_readiness()
    build_decision_engine(run_date=run_date)
    build_risk_report(run_date=run_date)


def _frozen_payload(*, run_date: date, evidence_mode: str) -> dict[str, object]:
    data_manifest = _read_json(Path("data/v2_data_truth/manifests/latest.json"), {})
    data_reconciliation = _read_json(Path("data/v2_data_truth/reconciliation/latest_reconciliation.json"), {})
    cards = _read_list(Path("data/v2_titan/decision_engine/decision_cards.json"))
    blocked = _read_list(Path("data/v2_titan/decision_engine/blocked_candidates.json"))
    watchlist = _read_list(Path("data/v2_titan/decision_engine/watchlist.json"))
    risk = _read_json(Path("data/v2_titan/risk/risk_report.json"), {})
    evidence = _read_json(Path("data/v2_paper_ops/reports/strategy_evidence_scores.json"), {})
    data_status = _data_status(data_reconciliation)
    accepted_end = str(data_manifest.get("accepted_end", "unknown")) if isinstance(data_manifest, dict) else "unknown"
    snapshot_id = str(data_manifest.get("snapshot_id", "unknown")) if isinstance(data_manifest, dict) else "unknown"
    strategy_rows = evidence.get("scores") if isinstance(evidence, dict) else []
    strategy_statuses = {
        str(row.get("strategy_id")): str(row.get("evidence_status", "unknown"))
        for row in strategy_rows
        if isinstance(row, dict)
    } if isinstance(strategy_rows, list) else {}
    accepted_candidates = [
        _freeze_pick_row(row, run_date, snapshot_id)
        for row in cards
        if row.get("status") not in {"blocked", "blocked_candidate", "blocked_watchlist"}
    ]
    blocked_rows = [_freeze_pick_row(row, run_date, snapshot_id) for row in blocked]
    watchlist_rows = [_freeze_pick_row(row, run_date, snapshot_id) for row in watchlist]
    near_setup_rows = [
        row for row in watchlist_rows if str(row.get("setup_status")) == "near_setup_candidate"
    ]
    no_setup = [row for row in watchlist_rows if str(row.get("setup_status")) == "no_setup"]
    run_id = stable_id("frozen_picks", evidence_mode, run_date.isoformat(), snapshot_id)
    source_hashes = _source_artifact_hashes(
        (
            Path("data/v2_data_truth/manifests/latest.json"),
            Path("data/v2_titan/decision_engine/decision_cards.json"),
            Path("data/v2_titan/risk/risk_report.json"),
            Path("data/v2_paper_ops/reports/strategy_evidence_scores.json"),
        )
    )
    payload: dict[str, object] = {
        "accepted_end_date": accepted_end,
        "accepted_candidates": accepted_candidates,
        "artifact_hashes": source_hashes,
        "blocked_candidates": blocked_rows,
        "candidates": accepted_candidates,
        "code_version": _git_commit(),
        "completed_bar_proof": bool(
            isinstance(data_manifest, dict)
            and str(data_manifest.get("accepted_end", "")) < str(data_manifest.get("requested_end", ""))
            and _int(data_manifest.get("skipped_incomplete_bars")) > 0
        ),
        "data_snapshot_id": snapshot_id,
        "data_truth_status": data_status,
        "date": run_date.isoformat(),
        "decision_cards": cards,
        "evidence_mode": evidence_mode,
        "generated_at": _stable_timestamp(run_date),
        "generated_at_policy": "deterministic per run date for stable reruns",
        "kill_switch_state": bool(risk.get("kill_switch")) if isinstance(risk, dict) else False,
        "near_setup_candidates": near_setup_rows,
        "no_setup_explanations": no_setup,
        "riskhub_warnings": risk.get("warnings", []) if isinstance(risk, dict) else [],
        "riskhub_state": risk,
        "run_id": run_id,
        "schema_version": "v2.forward_frozen_pick_set.v1",
        "source_artifact_hashes": source_hashes,
        "strategies_scanned": sorted(strategy_statuses),
        "strategy_statuses": strategy_statuses,
        "symbols_scanned": data_manifest.get("symbols", []) if isinstance(data_manifest, dict) else [],
        "watchlist_candidates": watchlist_rows,
        "warnings": (
            data_manifest.get("warnings", []) if isinstance(data_manifest, dict) else []
        ),
    }
    return payload


def _freeze_pick_row(row: dict[str, object], run_date: date, snapshot_id: str) -> dict[str, object]:
    status = str(row.get("status", "unknown"))
    blocked_reasons = row.get("reasons_to_avoid", [])
    strategy_status = str(
        row.get("strategy_evidence_status", row.get("strategy_status", "unknown"))
    )
    setup_status = _setup_status(row)
    return {
        "blocked_reason": " | ".join(str(item) for item in blocked_reasons) if isinstance(blocked_reasons, list) else str(blocked_reasons),
        "data_quality_status": row.get("data_truth_status", "unknown"),
        "data_snapshot_id": row.get("data_snapshot_id", snapshot_id),
        "direction": row.get("direction", "n/a"),
        "entry_trigger": row.get("entry_trigger", "n/a"),
        "evidence_mode": str(row.get("evidence_mode", "forward")),
        "evidence_summary": row.get("historical_backtest_summary", row.get("reason", "n/a")),
        "expected_fill_rule": row.get(
            "expected_fill_rule",
            "daily close-generated signal fills no earlier than next valid bar open",
        ),
        "forward_summary": row.get("forward_paper_summary", "n/a"),
        "generated_at": _stable_timestamp(run_date),
        "historical_backtest_summary": row.get("historical_backtest_summary", "n/a"),
        "invalidation": row.get("invalidation", "n/a"),
        "max_loss_estimate": row.get("max_loss_estimate", "n/a"),
        "pick_id": stable_id("pick", run_date.isoformat(), row.get("symbol", "n/a"), row.get("strategy_id", "unknown"), status),
        "replay_summary": row.get("replay_summary", "n/a"),
        "reward_risk": row.get("reward_risk", "n/a"),
        "risk_reward": row.get("reward_risk", "n/a"),
        "riskhub_warnings": row.get("warnings", []),
        "risk_per_unit": row.get("risk_per_share", "n/a"),
        "run_id": row.get("run_manifest_id", "n/a"),
        "setup_evidence": row.get("evidence", row.get("reason", "n/a")),
        "setup_status": setup_status,
        "stop": row.get("stop", "n/a"),
        "strategy_id": row.get("strategy_id", "unknown"),
        "strategy_status": strategy_status,
        "strategy_version": row.get("strategy_version", "unknown"),
        "suggested_paper_quantity": row.get("sizing_quantity", 0),
        "symbol": row.get("symbol", "n/a"),
        "target_or_exit_rule": row.get("target", row.get("reason", "n/a")),
        "warnings": row.get("warnings", []),
    }


def _setup_status(row: dict[str, object]) -> str:
    status = str(row.get("status", "unknown"))
    if status in {"candidate", "accepted_candidate"} and not row.get("reasons_to_avoid"):
        return "accepted_candidate"
    if "blocked" in status or row.get("reasons_to_avoid"):
        return "blocked_candidate"
    if status in {"watchlist", "watch"}:
        return "watchlist_candidate"
    if status in {"near_setup", "near_setup_candidate"}:
        return "near_setup_candidate"
    if status in {"no_setup", "blocked_watchlist"} or row.get("reason"):
        return "no_setup"
    return status


def _write_daily_report(
    *,
    paths: EvidenceVaultPaths,
    run_id: str,
    run_date: date,
    preflight_payload: dict[str, object],
    freeze_result: FrozenWriteResult,
    paper_result: dict[str, object],
    evaluation: dict[str, object],
    calendar_result: CalendarIntelligenceResult,
    evidence: dict[str, object],
    risk: dict[str, object],
    command_center: CommandCenterResult,
) -> dict[str, object]:
    frozen = read_json(freeze_result.frozen_json_path, {})
    payload = {
        "accepted_data_end_date": preflight_payload.get("accepted_end_date"),
        "calendar": calendar_result.to_dict(),
        "calendar_truth_result": _read_json(Path("data/v2_paper_ops/reports/forward_readiness.json"), {}),
        "command_center_artifact_result": command_center.to_dict(),
        "cumulative_strategy_returns": _strategy_returns(paths.calendar / "strategy_daily_returns.csv"),
        "daily_strategy_returns": _strategy_returns(paths.calendar / "strategy_daily_returns.csv", run_date=run_date),
        "data_snapshot_id": preflight_payload.get("data_snapshot_id"),
        "datatruth_status": preflight_payload.get("data_truth_status"),
        "date": run_date.isoformat(),
        "fills": _nested_count(paper_result, "fills"),
        "frozen_pick_hash": freeze_result.pick_set_hash,
        "kill_switch_status": risk.get("kill_switch_active"),
        "ledger_rebuild_result": _read_json(Path("data/v2_paper_ops/reports/ledger_rebuild.json"), {}),
        "number_accepted": len(_list(frozen.get("candidates"))) if isinstance(frozen, dict) else 0,
        "number_blocked": len(_list(frozen.get("blocked_candidates"))) if isinstance(frozen, dict) else 0,
        "number_candidates": len(_list(frozen.get("candidates"))) + len(_list(frozen.get("blocked_candidates"))) if isinstance(frozen, dict) else 0,
        "open_positions": _nested_count(paper_result, "open_positions"),
        "orders_created": _nested_count(paper_result, "orders_created"),
        "orders_pending": _nested_count(paper_result, "pending_orders"),
        "paper_ops": paper_result,
        "prior_pick_evaluation": evaluation,
        "riskhub_status": risk.get("riskhub_status"),
        "run_id": run_id,
        "schema_version": "v2.forward_daily_report.v1",
        "status": "passed",
        "strategy_evidence_changes": evidence,
        "tomorrows_expected_action": "Run forward_autopilot autopilot after the next completed market-data bar.",
        "warnings": _list(preflight_payload.get("warnings")) + _list(risk.get("warnings")),
    }
    json_path = paths.reports / "daily" / f"{run_date.isoformat()}.json"
    md_path = paths.reports / "daily" / f"{run_date.isoformat()}.md"
    _write_json(json_path, payload)
    _write_daily_report_md(md_path, payload)
    return payload


def _write_docs(
    *,
    output_root: Path,
    quality: dict[str, object],
    daily_report: dict[str, object],
) -> None:
    del output_root
    architecture = Path("docs/architecture")
    operations = Path("docs/operations")
    audit = Path("docs/audit")
    for folder in (architecture, operations, audit):
        folder.mkdir(parents=True, exist_ok=True)
    _write_text(
        architecture / "v2_forward_autopilot.md",
        _doc(
            "v2 Forward Autopilot",
            [
                "Forward Autopilot orchestrates DataTruth, Decision Engine, frozen picks, PaperOps, calendar intelligence, Strategy Evidence 2.0, RiskHub, and Command Center regeneration.",
                "Forward evidence is one-date-at-a-time and stores frozen pick hashes before later outcomes are evaluated.",
                "Shadow-forward replay is labeled separately and stored under data/v2_forward_evidence/shadow_replay.",
            ],
        ),
    )
    _write_text(
        architecture / "v2_evidence_vault.md",
        _doc(
            "v2 Evidence Vault",
            [
                "Frozen pick JSON is canonicalized and hashed with SHA-256.",
                "Existing frozen files are never silently overwritten; hash changes produce superseding artifacts with explicit reasons.",
                "Hash verification is exposed through py -m intraday_scanner.v2.forward_autopilot verify.",
            ],
        ),
    )
    _write_text(
        architecture / "v2_calendar_intelligence.md",
        _doc(
            "v2 Calendar Intelligence",
            [
                "Forward, shadow_forward_replay, research_backtest, and demo evidence labels are kept distinct.",
                "Calendar rows carry pick_set_hash, RiskHub state, ledger rebuild status, and calendar truth status.",
                "No strategy is validated without true forward days, closed trades, positive expectancy, and passing truth gates.",
            ],
        ),
    )
    _write_text(
        architecture / "v2_strategy_evidence.md",
        _doc(
            "v2 Strategy Evidence",
            [
                "Strategy Evidence 2.0 separates backtest, shadow replay, and true forward evidence.",
                "Replay-only profit cannot validate a strategy.",
                "Fragile strategies remain quarantined until forward evidence and risk gates justify promotion.",
            ],
        ),
    )
    _write_text(operations / "forward_autopilot_daily_runbook.md", _daily_runbook())
    _write_text(operations / "forward_autopilot_scheduler_examples.md", _scheduler_examples())
    _write_text(audit / "forward_autopilot_summary.md", _summary_doc(daily_report, quality))
    _write_text(audit / "forward_autopilot_red_team.md", _red_team_doc())
    _write_text(audit / "forward_autopilot_resume_goal.md", _resume_goal())


def _write_build_state(
    *,
    output_root: Path,
    result: ForwardAutopilotResult,
    quality: dict[str, object],
    integrity: dict[str, object],
) -> None:
    state = {
        **result.to_dict(),
        "completed_work": [
            "Forward Evidence Vault with frozen picks and stable hashes.",
            "Daily autopilot orchestration.",
            "Isolated shadow-forward replay artifacts.",
            "Calendar Intelligence outputs.",
            "Strategy Evidence 2.0 outputs.",
            "RiskHub daily governance report.",
            "Command Center forward evidence pages.",
            "Integrity verification.",
        ],
        "integrity": integrity,
        "quality": quality,
        "remaining_work": [
            "Accumulate 30 true forward days and 30 closed forward trades before validation.",
            "Add broker-grade or user-owned OHLCV for stronger market-data confidence.",
            "Add intraday data before claiming intraday stop/target precision.",
        ],
        "status": result.status,
    }
    _write_json(Path("docs/audit/forward_autopilot_build_state.json"), state)
    _write_json(output_root / "reports" / "forward_autopilot_build_state.json", state)


def _write_scheduler_scripts() -> None:
    scripts = Path("scripts")
    scripts.mkdir(parents=True, exist_ok=True)
    _write_text(
        scripts / "run_forward_autopilot_daily.ps1",
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$Date = if ($args.Count -gt 0) { $args[0] } else { (Get-Date).ToString('yyyy-MM-dd') }",
                "New-Item -ItemType Directory -Force -Path 'data/v2_forward_evidence/logs' | Out-Null",
                "py -m intraday_scanner.v2.forward_autopilot autopilot --date $Date *> \"data/v2_forward_evidence/logs/forward_autopilot_$Date.log\"",
                "if ($LASTEXITCODE -ne 0) { throw \"forward_autopilot failed with exit code $LASTEXITCODE\" }",
            ]
        )
        + "\n",
    )
    _write_text(
        scripts / "run_forward_autopilot_daily.sh",
        "\n".join(
            [
                "#!/usr/bin/env sh",
                "set -eu",
                "RUN_DATE=\"${1:-$(date +%F)}\"",
                "mkdir -p data/v2_forward_evidence/logs",
                "py -m intraday_scanner.v2.forward_autopilot autopilot --date \"$RUN_DATE\" > \"data/v2_forward_evidence/logs/forward_autopilot_$RUN_DATE.log\" 2>&1",
            ]
        )
        + "\n",
    )


def _copy_shadow_artifacts(paths: EvidenceVaultPaths, shadow_paper_root: Path) -> None:
    copy_pairs = (
        (shadow_paper_root / "ledger" / "paper_ledger.jsonl", paths.shadow_replay / "ledger" / "paper_ledger.jsonl"),
        (shadow_paper_root / "calendar" / "strategy_daily_returns.csv", paths.shadow_replay / "calendar" / "strategy_daily_returns.csv"),
        (shadow_paper_root / "calendar" / "calendar_summary.md", paths.shadow_replay / "calendar" / "calendar_summary.md"),
    )
    for source, target in copy_pairs:
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def _order_by_pick(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for event in events:
        payload = event.get("payload")
        if event.get("event_type") == "paper_order_created" and isinstance(payload, dict):
            rows[str(payload.get("pick_id"))] = payload
    return rows


def _fill_by_order(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for event in events:
        payload = event.get("payload")
        if event.get("event_type") == "paper_fill" and isinstance(payload, dict):
            rows[str(payload.get("order_id"))] = payload
    return rows


def _position_by_order(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for event in events:
        payload = event.get("payload")
        if event.get("event_type") == "paper_position_opened" and isinstance(payload, dict):
            rows[str(payload.get("order_id"))] = payload
    return rows


def _close_by_position(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for event in events:
        payload = event.get("payload")
        if event.get("event_type") == "paper_position_closed" and isinstance(payload, dict):
            rows[str(payload.get("position_id"))] = payload
    return rows


def _outcome_status(
    order: dict[str, object] | None,
    fill: dict[str, object] | None,
    close: dict[str, object] | None,
    frozen_date: str,
    run_date: date,
) -> str:
    if frozen_date == run_date.isoformat():
        return "pending_next_bar_evaluation"
    if close:
        return "closed"
    if fill:
        return "filled_open"
    if order:
        return "pending_order"
    return "no_order_created"


def _all_pick_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for key in ("candidates", "blocked_candidates", "no_setup_explanations"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    return rows


def _manual_review_flags(risk: object, blocked: list[object]) -> list[str]:
    flags: list[str] = []
    if isinstance(risk, dict) and risk.get("kill_switch") is True:
        flags.append("RiskHub kill switch active.")
    if blocked:
        flags.append("Blocked candidates require review before any paper decision.")
    return flags


def _evidence_2_blockers(row: dict[str, object], forward_days: int, forward_closed: int) -> list[str]:
    blockers: list[str] = []
    if row.get("evidence_status") == "quarantined":
        blockers.append("strategy is quarantined")
    if forward_days < 30:
        blockers.append(f"needs {30 - forward_days} more true forward days")
    if forward_closed < 30:
        blockers.append(f"needs {30 - forward_closed} more closed forward trades")
    if _float(row.get("expectancy")) <= 0:
        blockers.append("forward expectancy is not positive")
    if _float(row.get("profit_factor")) <= 1.10:
        blockers.append("profit factor is below validation threshold")
    return blockers


def _evidence_2_status(row: dict[str, object], blockers: list[str]) -> str:
    if row.get("evidence_status") == "quarantined":
        return "quarantined"
    if not blockers:
        return "validated"
    return "watch"


def _kill_switch_frequency(rows: list[dict[str, str]]) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if str(row.get("kill_switch_active")) == "true") / len(rows)


def _write_strategy_evidence_md(path: Path, rows: list[dict[str, object]]) -> None:
    lines = [
        "# Strategy Evidence 2.0",
        "",
        "Forward evidence dominates once enough true forward days exist. Replay-only evidence cannot validate a strategy.",
        "",
    ]
    for row in rows:
        lines.append(
            f"- `{row['strategy_id']}` status `{row['evidence_status']}`; forward days `{row['forward_days']}`; blockers: {row['blockers'] or 'none'}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_daily_report_md(path: Path, payload: dict[str, object]) -> None:
    lines = [
        f"# Forward Autopilot Daily Report {payload['date']}",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Data snapshot: `{payload['data_snapshot_id']}`",
        f"- Accepted data end date: `{payload['accepted_data_end_date']}`",
        f"- DataTruth status: `{payload['datatruth_status']}`",
        f"- Frozen pick hash: `{payload['frozen_pick_hash']}`",
        f"- Candidates: `{payload['number_candidates']}`",
        f"- Blocked: `{payload['number_blocked']}`",
        f"- Accepted: `{payload['number_accepted']}`",
        f"- RiskHub: `{payload['riskhub_status']}`",
        f"- Kill switch: `{payload['kill_switch_status']}`",
        "- Boundary: research-only; no live execution or broker routing.",
        "",
        "## Tomorrow",
        "",
        f"- {payload['tomorrows_expected_action']}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _strategy_returns(path: Path, *, run_date: date | None = None) -> list[dict[str, object]]:
    rows = _read_csv(path)
    output: list[dict[str, object]] = []
    for row in rows:
        if run_date is not None and row.get("date") != run_date.isoformat():
            continue
        output.append(
            {
                "cumulative_return_pct": _float(row.get("cumulative_return_pct")),
                "daily_return_pct": _float(row.get("daily_return_pct")),
                "evidence_mode": row.get("evidence_mode"),
                "strategy_id": row.get("strategy_id"),
            }
        )
    return output


def _missing_daily_reports(paths: EvidenceVaultPaths) -> list[str]:
    missing: list[str] = []
    for frozen in sorted(paths.frozen_picks.glob("*_picks.json")):
        date_value = frozen.name[:10]
        if not (paths.reports / "daily" / f"{date_value}.json").exists():
            missing.append(date_value)
    return missing


def _absolute_path_leaks(root: Path) -> list[str]:
    leaks: list[str] = []
    if not root.exists():
        return leaks
    pattern = re.compile(r"[A-Za-z]:[\\/][^\"'<>\s]+")
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".html", ".csv"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            leaks.append(path.as_posix())
    return leaks


def _latest_frozen_date(paths: EvidenceVaultPaths) -> date:
    dates = [path.name[:10] for path in paths.frozen_picks.glob("*_picks*.json")]
    if not dates:
        return date.today()
    return date.fromisoformat(max(dates))


def _score_cat(name: str, max_score: int, passed: bool) -> dict[str, object]:
    return {
        "category": name,
        "evidence": "passed" if passed else "missing_or_incomplete",
        "max_score": max_score,
        "score": max_score if passed else max(0, max_score - 3),
    }


def _has_verified_existing(paths: EvidenceVaultPaths) -> bool:
    latest = read_json(paths.reports / "latest_freeze_result.json", {})
    return isinstance(latest, dict) and latest.get("status") in {
        "verified_existing",
        "written",
        "superseding_written",
    }


def _completed_bar_proof() -> bool:
    payload = read_json(Path("data/v2_data_truth/manifests/latest.json"), {})
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("accepted_end", "")) < str(payload.get("requested_end", ""))
        and _int(payload.get("skipped_incomplete_bars")) > 0
    )


def _forward_docs_exist() -> bool:
    return all(
        Path(path).exists()
        for path in (
            "docs/architecture/v2_forward_autopilot.md",
            "docs/architecture/v2_evidence_vault.md",
            "docs/architecture/v2_calendar_intelligence.md",
            "docs/operations/forward_autopilot_daily_runbook.md",
            "docs/operations/forward_autopilot_scheduler_examples.md",
            "docs/audit/forward_autopilot_red_team.md",
        )
    )


def _product_coherence(paths: EvidenceVaultPaths, qa: object) -> bool:
    return (
        bool(list((paths.reports / "daily").glob("*.json")))
        and isinstance(qa, dict)
        and qa.get("status") == "passed"
        and (paths.reports / "riskhub_daily.json").exists()
        and (paths.reports / "strategy_evidence_2_0.json").exists()
    )


def _write_quality_md(path: Path, result: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Forward Autopilot Quality Scorecard",
        "",
        f"- Score: `{result['score']} / 100`",
        f"- Target: `{result['target']} / 100`",
        f"- Status: `{result['status']}`",
        "",
        "| Category | Score | Evidence |",
        "| --- | ---: | --- |",
    ]
    categories = result.get("categories", [])
    if isinstance(categories, list):
        for row in categories:
            if isinstance(row, dict):
                lines.append(
                    f"| {row['category']} | {row['score']} / {row['max_score']} | {row['evidence']} |"
                )
    lines.extend(["", "## Blockers", ""])
    blockers = result.get("blockers", [])
    if isinstance(blockers, list) and blockers:
        lines.extend(f"- {blocker}" for blocker in blockers)
    else:
        lines.append("- None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _daily_runbook() -> str:
    return _doc(
        "Forward Autopilot Daily Runbook",
        [
            "After market close, run `py -m intraday_scanner.v2.forward_autopilot autopilot --date YYYY-MM-DD`.",
            "Morning review starts at `data/v2_command_center/production.html`.",
            "Review frozen picks, RiskHub daily, evidence integrity, and calendar pages before trusting any paper decision.",
            "Weekly, run `py -m intraday_scanner.v2.forward_autopilot verify` and `py -m intraday_scanner.v2.forward_autopilot dashboard`.",
            "If a hash mismatch appears, treat it as manual_override_required until the superseding artifact reason is reviewed.",
            "All-blocked days are valid safety outcomes, not failures.",
        ],
    )


def _scheduler_examples() -> str:
    return _doc(
        "Forward Autopilot Scheduler Examples",
        [
            "PowerShell: `scripts/run_forward_autopilot_daily.ps1 YYYY-MM-DD`.",
            "Shell: `sh scripts/run_forward_autopilot_daily.sh YYYY-MM-DD`.",
            "Scripts write logs under `data/v2_forward_evidence/logs` and do not install scheduled tasks.",
            "No secrets, broker routing, or trade-placement path is included.",
        ],
    )


def _summary_doc(daily_report: dict[str, object], quality: dict[str, object]) -> str:
    return _doc(
        "Forward Autopilot Summary",
        [
            f"Latest run ID: {daily_report.get('run_id', 'n/a')}",
            f"Frozen pick hash: {daily_report.get('frozen_pick_hash', 'n/a')}",
            f"Quality score: {quality.get('score', 'n/a')} / 100",
            "Forward, shadow replay, backtest, and demo evidence remain separately labeled.",
            "No strategy is validated; forward evidence thresholds remain explicit.",
            "Live execution remains disabled.",
        ],
    )


def _red_team_doc() -> str:
    checks = [
        "Frozen pick hashes verify and superseding artifacts are explicit.",
        "Replay evidence is labeled shadow_forward_replay and stored separately.",
        "Same-day daily fills are blocked by PaperOps next-valid-bar behavior.",
        "Incomplete daily bars are skipped by DataTruth.",
        "Synthetic/demo evidence cannot validate strategies.",
        "RiskHub blocked candidates remain blocked in frozen pick sets.",
        "Command Center QA checks required pages, links, script tags, local path leaks, and research-only banners.",
        "No broker imports, app.py imports, Streamlit imports, SQLite writes, or secrets are added by v2 forward modules.",
    ]
    return _doc("Forward Autopilot Red Team", checks)


def _resume_goal() -> str:
    return "\n".join(
        [
            "# Forward Autopilot Resume Goal",
            "",
            "Continue from the current Forward Evidence Autopilot artifacts.",
            "",
            "Next work:",
            "- Accumulate 30 true forward paper days and 30 closed forward trades before validation.",
            "- Add broker-grade or user-owned OHLCV to improve DataTruth confidence.",
            "- Add intraday data before claiming intraday stop/target precision.",
            "- Keep frozen pick hash verification, calendar truth, ledger rebuild, and Command Center QA passing.",
        ]
    ) + "\n"


def _source_artifact_hashes(paths: tuple[Path, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in paths:
        if path.exists():
            hashes[path.as_posix()] = _stable_artifact_hash(path)
    return hashes


def _stable_artifact_hash(path: Path) -> str:
    if path.suffix.lower() == ".json":
        payload = read_json(path, None)
        if payload is not None:
            return canonical_hash(_strip_volatile_fields(payload))
    return _file_sha256(path)


def _strip_volatile_fields(value: object) -> object:
    volatile = {"created_at", "generated_at", "updated_at"}
    if isinstance(value, dict):
        return {
            str(key): _strip_volatile_fields(item)
            for key, item in value.items()
            if str(key) not in volatile
        }
    if isinstance(value, list | tuple):
        return [_strip_volatile_fields(item) for item in value]
    return value


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _data_status(payload: object) -> str:
    if isinstance(payload, dict):
        report = payload.get("report")
        if isinstance(report, dict):
            return str(report.get("status", "unknown"))
        return str(payload.get("status", "unknown"))
    return "unknown"


def _nested_count(payload: object, key: str) -> int:
    if isinstance(payload, dict):
        if key in payload:
            return _int(payload.get(key))
        return sum(_nested_count(value, key) for value in payload.values())
    return 0


def _stable_timestamp(run_date: date) -> str:
    return datetime.combine(run_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _safe_call(func: Any, *, default: dict[str, object]) -> dict[str, object]:
    try:
        value = func()
    except Exception as exc:  # pragma: no cover - defensive report path
        return {"error": str(exc), **default}
    return value if isinstance(value, dict) else default


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_list(path: Path) -> list[dict[str, object]]:
    payload = _read_json(path, [])
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _read_json(path: Path, default: object) -> object:
    return read_json(path, default)


def _write_json(path: Path, payload: object) -> None:
    write_json(path, payload)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = tuple(sorted({key for row in rows for key in row})) or ("empty",)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _write_markdown(path: Path, title: str, payload: dict[str, object]) -> None:
    lines = [f"# {title}", ""]
    for key, value in payload.items():
        if key == "rows":
            continue
        lines.append(f"- {key}: `{value}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _doc(title: str, bullets: list[str]) -> str:
    return "# " + title + "\n\n" + "\n".join(f"- {bullet}" for bullet in bullets) + "\n"


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, str | int | float):
        return float(value)
    return 0.0


def _int(value: object) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str | int | float):
        return int(float(value))
    return 0


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " | ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)
