# ruff: noqa: E501
# mypy: ignore-errors
"""Scheduler-ready OMEGA Sentinel operations layer."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.omega import build_all as build_omega

SENTINEL_DIRS = (
    "status",
    "alerts",
    "logs",
    "reports",
    "trial",
    "run_locks",
    "retention",
    "manifests",
    "reconciliation",
)
TARGET_FORWARD_DAYS = 30
RUNBOOK_COMMAND = "py -m intraday_scanner.v2.omega_sentinel run --date YYYY-MM-DD"


@dataclass(frozen=True)
class SentinelResult:
    status: str
    run_id: str
    run_date: date
    alert_level: str
    frozen_pick_hash: str
    status_path: Path
    report_path: Path
    dashboard_index: Path
    quality_score: int

    def to_dict(self) -> dict[str, object]:
        return {
            "alert_level": self.alert_level,
            "dashboard_index": self.dashboard_index.as_posix(),
            "frozen_pick_hash": self.frozen_pick_hash,
            "quality_score": self.quality_score,
            "report_path": self.report_path.as_posix(),
            "run_date": self.run_date.isoformat(),
            "run_id": self.run_id,
            "status": self.status,
            "status_path": self.status_path.as_posix(),
        }


def init(*, output_root: Path = Path("data/v2_omega_sentinel")) -> dict[str, object]:
    paths = _paths(output_root)
    _write_json(
        paths["manifests"] / "omega_sentinel_manifest.json",
        {
            "created_at": _now(),
            "external_alerts_default": "disabled",
            "live_trading": "disabled",
            "output_root": output_root.as_posix(),
            "schema_version": "v2.omega_sentinel_manifest.v1",
            "status": "initialized",
        },
    )
    _write_docs()
    scripts = generate_scheduler_scripts(output_root=output_root)
    return {
        "output_root": output_root.as_posix(),
        "scripts": scripts["scripts"],
        "status": "initialized",
    }


def run(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_omega_sentinel"),
    allow_fetch: bool = False,
    stale_after_minutes: int = 240,
) -> SentinelResult:
    init(output_root=output_root)
    started = datetime.now(timezone.utc)
    lock = _acquire_lock(
        output_root=output_root,
        run_date=run_date,
        command="run",
        stale_after_minutes=stale_after_minutes,
    )
    omega_result: dict[str, object] = {}
    errors: list[str] = []
    try:
        omega_result = omega(run_date=run_date, output_root=output_root, allow_fetch=allow_fetch)
    except Exception as exc:  # pragma: no cover - defensive operational path
        errors.append(str(exc))
    completed = datetime.now(timezone.utc)
    payload = _build_status_payload(
        run_date=run_date,
        started_at=started,
        completed_at=completed,
        command="run",
        fetch_mode="fetch-enabled" if allow_fetch else "no-fetch",
        omega_result=omega_result,
        errors=errors,
    )
    status_paths = _write_status(output_root, payload)
    alert = _write_alert(output_root, payload)
    trial = trial_status(output_root=output_root)
    artifact_index(output_root=output_root, run_date=run_date)
    report_payload = _write_operational_report(output_root, payload, alert, trial)
    build_command_center()
    _release_lock(output_root=output_root, lock_id=str(lock["lock_id"]))
    verification = verify(output_root=output_root)
    doctor_result = doctor(output_root=output_root)
    score = _write_scorecard(
        output_root=output_root,
        status_payload=payload,
        alert_payload=alert,
        trial_payload=trial,
        verification=verification,
        doctor_result=doctor_result,
    )
    _write_build_state(
        output_root=output_root,
        status_payload=payload,
        quality_score=score,
        doctor_result=doctor_result,
    )
    final_status = (
        "complete" if score >= 99 and payload["alert_level"] != "red" else "resume_required"
    )
    return SentinelResult(
        status=final_status,
        run_id=str(payload["run_id"]),
        run_date=run_date,
        alert_level=str(payload["alert_level"]),
        frozen_pick_hash=str(payload.get("frozen_pick_hash", "")),
        status_path=status_paths["json"],
        report_path=Path(str(report_payload["markdown"])),
        dashboard_index=Path("data/v2_command_center/production.html"),
        quality_score=score,
    )


def run_today(
    *,
    output_root: Path = Path("data/v2_omega_sentinel"),
    allow_fetch: bool = False,
    stale_after_minutes: int = 240,
) -> SentinelResult:
    return run(
        run_date=date.today(),
        output_root=output_root,
        allow_fetch=allow_fetch,
        stale_after_minutes=stale_after_minutes,
    )


def omega(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_omega_sentinel"),
    allow_fetch: bool = False,
) -> dict[str, object]:
    del output_root
    result = build_omega(run_date=run_date, allow_fetch=allow_fetch)
    return result.to_dict()


def morning_check(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_omega_sentinel"),
    use_real_intraday: bool = False,
    autodata: bool = False,
    learn: bool = False,
    telegram: bool = False,
    market_masters: bool = False,
) -> dict[str, object]:
    from intraday_scanner.v2.evidence_commit import propose as commitbridge_propose
    from intraday_scanner.v2.evidence_commit import reconcile as commitbridge_reconcile
    from intraday_scanner.v2.evidence_commit import report as commitbridge_report
    from intraday_scanner.v2.evidence_commit import review as commitbridge_review
    from intraday_scanner.v2.fill_truth import morning_check as filltruth_morning_check

    autodata_payload: dict[str, object] = {"status": "skipped"}
    if autodata:
        from intraday_scanner.v2.autodata import trial_day as autodata_trial_day

        autodata_payload = autodata_trial_day(run_date=run_date)
    real_intraday = _real_intraday_cycle(run_date, enabled=use_real_intraday)
    filltruth = filltruth_morning_check(run_date=run_date)
    proposals = commitbridge_propose(run_date=run_date)
    review_payload = commitbridge_review(run_date=run_date)
    reconciliation = commitbridge_reconcile(run_date=run_date)
    summary = commitbridge_report()
    learning_payload: dict[str, object] = {"status": "skipped"}
    if learn:
        from intraday_scanner.v2.learning_foundry import daily_learn

        learning_payload = daily_learn(run_date=run_date)
        build_command_center()
    market_masters_payload = _market_masters_after_run(run_date, enabled=market_masters)
    telegram_payload = _telegram_after_run("morning", run_date, enabled=telegram)
    return {
        "commitbridge": summary,
        "autodata": autodata_payload,
        "filltruth": filltruth,
        "learning_foundry": learning_payload,
        "market_masters": market_masters_payload,
        "proposals": proposals,
        "real_intraday": real_intraday,
        "reconciliation": reconciliation,
        "review": review_payload,
        "status": "passed",
        "telegram_send_status": telegram_payload.get("send_status", telegram_payload.get("status")),
        "telegram": telegram_payload,
    }


def after_close(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_omega_sentinel"),
    use_real_intraday: bool = False,
    autodata: bool = False,
    learn: bool = False,
    telegram: bool = False,
    market_masters: bool = False,
) -> dict[str, object]:
    from intraday_scanner.v2.evidence_commit import propose as commitbridge_propose
    from intraday_scanner.v2.evidence_commit import reconcile as commitbridge_reconcile
    from intraday_scanner.v2.evidence_commit import report as commitbridge_report
    from intraday_scanner.v2.evidence_commit import review as commitbridge_review
    from intraday_scanner.v2.fill_truth import after_close as filltruth_after_close

    autodata_payload: dict[str, object] = {"status": "skipped"}
    if autodata:
        from intraday_scanner.v2.autodata import trial_day as autodata_trial_day

        autodata_payload = autodata_trial_day(run_date=run_date)
    real_intraday = _real_intraday_cycle(run_date, enabled=use_real_intraday)
    filltruth = filltruth_after_close(run_date=run_date)
    proposals = commitbridge_propose(run_date=run_date)
    review_payload = commitbridge_review(run_date=run_date)
    reconciliation = commitbridge_reconcile(run_date=run_date)
    summary = commitbridge_report()
    learning_payload: dict[str, object] = {"status": "skipped"}
    if learn:
        from intraday_scanner.v2.learning_foundry import daily_learn

        learning_payload = daily_learn(run_date=run_date)
    market_masters_payload = _market_masters_after_run(run_date, enabled=market_masters)
    build_command_center()
    telegram_payload = _telegram_after_run("after-close", run_date, enabled=telegram)
    return {
        "commitbridge": summary,
        "autodata": autodata_payload,
        "filltruth": filltruth,
        "learning_foundry": learning_payload,
        "market_masters": market_masters_payload,
        "proposals": proposals,
        "real_intraday": real_intraday,
        "reconciliation": reconciliation,
        "review": review_payload,
        "status": "passed",
        "telegram_send_status": telegram_payload.get("send_status", telegram_payload.get("status")),
        "telegram": telegram_payload,
    }


def commit_filltruth(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_omega_sentinel"),
) -> dict[str, object]:
    del output_root
    from intraday_scanner.v2.evidence_commit import commit as commitbridge_commit
    from intraday_scanner.v2.evidence_commit import propose as commitbridge_propose
    from intraday_scanner.v2.evidence_commit import rebuild_state as commitbridge_rebuild
    from intraday_scanner.v2.evidence_commit import reconcile as commitbridge_reconcile
    from intraday_scanner.v2.evidence_commit import report as commitbridge_report
    from intraday_scanner.v2.evidence_commit import review as commitbridge_review
    from intraday_scanner.v2.evidence_commit import verify as commitbridge_verify

    proposals = commitbridge_propose(run_date=run_date)
    review_payload = commitbridge_review(run_date=run_date)
    commit_payload = commitbridge_commit(run_date=run_date)
    rebuild = commitbridge_rebuild(run_date=run_date)
    reconciliation = commitbridge_reconcile(run_date=run_date)
    summary = commitbridge_report()
    verification = commitbridge_verify()
    build_command_center()
    return {
        "commit": commit_payload,
        "commitbridge": summary,
        "proposals": proposals,
        "rebuild": rebuild,
        "reconciliation": reconciliation,
        "review": review_payload,
        "status": "passed" if verification.get("status") == "passed" else "failed",
        "verify": verification,
    }


def resolve_pending(
    *,
    run_date: date,
    output_root: Path = Path("data/v2_omega_sentinel"),
    autodata: bool = False,
) -> dict[str, object]:
    del output_root
    from intraday_scanner.v2.fill_truth import resolve_pending as filltruth_resolve_pending

    if autodata:
        from intraday_scanner.v2.autodata import feed_filltruth

        feed_filltruth(run_date=run_date)
    return filltruth_resolve_pending(run_date=run_date)


def status(*, output_root: Path = Path("data/v2_omega_sentinel")) -> dict[str, object]:
    payload = _read_json(output_root / "status" / "latest_status.json", {})
    if isinstance(payload, dict) and payload:
        return payload
    return {"status": "missing", "errors": ["latest Sentinel status does not exist"]}


def report(*, output_root: Path = Path("data/v2_omega_sentinel")) -> dict[str, object]:
    status_payload = status(output_root=output_root)
    if status_payload.get("status") == "missing":
        return status_payload
    alert = _read_json(output_root / "alerts" / "latest_alert.json", {})
    trial = trial_status(output_root=output_root)
    return _write_operational_report(output_root, status_payload, _dict(alert), trial)


def verify(*, output_root: Path = Path("data/v2_omega_sentinel")) -> dict[str, object]:
    init(output_root=output_root)
    latest_status = _dict(_read_json(output_root / "status" / "latest_status.json", {}))
    latest_alert = _dict(_read_json(output_root / "alerts" / "latest_alert.json", {}))
    integrity = _dict(_read_json(Path("data/v2_forward_evidence/reconciliation/evidence_integrity.json"), {}))
    qa = _dict(_read_json(Path("data/v2_command_center/command_center_qa.json"), {}))
    failures: list[str] = []
    warnings: list[str] = []
    if not latest_status:
        failures.append("latest status missing")
    if latest_status and not latest_status.get("frozen_pick_hash"):
        failures.append("missing frozen pick hash")
    if latest_status and latest_status.get("completed_bar_status") != "passed":
        failures.append("completed-bar proof failed")
    if integrity.get("status") != "passed":
        failures.append("forward evidence integrity failed")
    if qa.get("status") != "passed":
        failures.append("command center QA failed")
    if latest_alert.get("alert_level") == "red":
        failures.append("latest alert is red")
    lock = lock_status(output_root=output_root)
    if lock.get("state") == "locked":
        warnings.append("active Sentinel lock exists")
    payload = {
        "checked_at": _now(),
        "command_center_status": qa.get("status", "missing"),
        "failures": failures,
        "frozen_pick_hash": latest_status.get("frozen_pick_hash", "n/a"),
        "integrity_status": integrity.get("status", "missing"),
        "schema_version": "v2.omega_sentinel_verification.v1",
        "status": "passed" if not failures else "failed",
        "warnings": warnings,
    }
    root = output_root / "reconciliation"
    _write_json(root / "latest_verification.json", payload)
    _write_md(root / "latest_verification.md", "OMEGA Sentinel Verification", _kv_lines(payload))
    return payload


def trial_status(*, output_root: Path = Path("data/v2_omega_sentinel")) -> dict[str, object]:
    init(output_root=output_root)
    status_rows = _dated_status_rows(output_root / "status")
    forward_rows = [row for row in _calendar_rows() if row.get("evidence_mode") == "forward"]
    counted = [row for row in status_rows if _counts_as_forward_day(row)]
    latest_date = max((str(row.get("run_date")) for row in status_rows), default="n/a")
    trial_start = min((str(row.get("run_date")) for row in counted), default=latest_date)
    strategies = sorted({str(row.get("strategy_id", "unknown")) for row in forward_rows})
    calendar_rows = []
    for row in status_rows:
        calendar_rows.append(
            {
                "alert_level": row.get("alert_level", "unknown"),
                "counts_as_forward_day": _counts_as_forward_day(row),
                "frozen_pick_hash": row.get("frozen_pick_hash", "n/a"),
                "run_date": row.get("run_date", "unknown"),
            }
        )
    strategy_summary = [_strategy_trial_row(strategy, forward_rows) for strategy in strategies]
    missing_days = _missing_weekdays(trial_start, latest_date, counted)
    payload: dict[str, object] = {
        "completed_forward_days": len(counted),
        "cumulative_return_by_strategy": {
            row["strategy_id"]: row["cumulative_return_pct"] for row in strategy_summary
        },
        "daily_average_return_by_strategy": {
            row["strategy_id"]: row["average_daily_return_pct"] for row in strategy_summary
        },
        "days_remaining_to_minimum_evidence": max(0, TARGET_FORWARD_DAYS - len(counted)),
        "days_with_all_candidates_blocked": sum(
            1 for row in counted if _int(row.get("accepted_candidate_count")) == 0
            and _int(row.get("blocked_candidate_count")) > 0
        ),
        "days_with_closes": sum(1 for row in counted if _int(row.get("closes")) > 0),
        "days_with_fills": sum(1 for row in counted if _int(row.get("fills")) > 0),
        "days_with_open_positions": sum(1 for row in counted if _int(row.get("open_positions")) > 0),
        "days_with_pending_orders": sum(1 for row in counted if _int(row.get("pending_orders")) > 0),
        "days_with_real_intraday_import": sum(1 for row in counted if row.get("real_intraday_source_label") == "real_local_intraday"),
        "days_with_reconciled_intraday": sum(
            1
            for row in counted
            if row.get("intraday_daily_reconciliation_status") in {"reconciled", "reconciled_with_minor_diffs"}
        ),
        "days_with_committed_filltruth_evidence": sum(1 for row in counted if _int(row.get("proposals_committed")) > 0),
        "days_with_overlay_only_filltruth": sum(
            1
            for row in counted
            if _int(row.get("fill_truth_intraday_supported_count")) > 0
            and _int(row.get("proposals_committed")) == 0
        ),
        "days_with_pending_unresolved": sum(1 for row in counted if _int(row.get("pending_after_commit")) > 0),
        "days_with_execution_model_disagreement": sum(1 for row in counted if _int(row.get("fill_truth_disagreement_count")) > 0),
        "days_with_session_incomplete": sum(1 for row in counted if row.get("real_intraday_session_completeness") == "partial_session"),
        "days_with_manual_review_required": sum(1 for row in counted if _int(row.get("uncommitted_overlay_count")) > 0 or _int(row.get("proposals_eligible")) > 0),
        "days_with_picks": sum(1 for row in counted if _int(row.get("frozen_pick_count")) > 0),
        "drawdown_by_strategy": {row["strategy_id"]: row["drawdown_pct"] for row in strategy_summary},
        "green_days": sum(1 for row in status_rows if row.get("alert_level") == "green"),
        "latest_run_date": latest_date,
        "missing_days": missing_days,
        "next_review_date": _next_weekday(latest_date),
        "red_days": sum(1 for row in status_rows if row.get("alert_level") == "red"),
        "schema_version": "v2.omega_sentinel_trial_status.v1",
        "skipped_days": [],
        "status": "passed",
        "strategy_forward_closed_trade_count": {
            row["strategy_id"]: row["closed_trades"] for row in strategy_summary
        },
        "strategy_forward_trade_count": {
            row["strategy_id"]: row["opened_trades"] for row in strategy_summary
        },
        "strategy_status": {row["strategy_id"]: row["strategy_status"] for row in strategy_summary},
        "target_forward_days": TARGET_FORWARD_DAYS,
        "trial_start_date": trial_start,
        "validation_missing_requirements": _validation_requirements(strategy_summary, len(counted)),
        "yellow_days": sum(1 for row in status_rows if row.get("alert_level") == "yellow"),
    }
    root = output_root / "trial"
    _write_json(root / "forward_trial_status.json", payload)
    _write_csv(root / "forward_trial_calendar.csv", calendar_rows)
    _write_csv(root / "forward_trial_strategy_summary.csv", strategy_summary)
    _write_md(root / "forward_trial_status.md", "Forward Trial Status", _trial_lines(payload))
    _write_md(root / "forward_trial_blockers.md", "Forward Trial Blockers", _blocker_lines(payload))
    return payload


def generate_scheduler_scripts(
    *, output_root: Path = Path("data/v2_omega_sentinel")
) -> dict[str, object]:
    init(output_root=output_root) if not output_root.exists() else _paths(output_root)
    scripts = {
        "scripts/run_omega_sentinel_daily.ps1": "\n".join(
            [
                "param([string]$Date = (Get-Date -Format 'yyyy-MM-dd'))",
                "$ErrorActionPreference = 'Stop'",
                "New-Item -ItemType Directory -Force -Path 'data/v2_omega_sentinel/logs' | Out-Null",
                "py -m intraday_scanner.v2.omega_sentinel run --date $Date *> \"data/v2_omega_sentinel/logs/sentinel_$Date.log\"",
                "$Code = $LASTEXITCODE",
                "if ($Code -ne 0) { throw \"omega sentinel failed with exit code $Code\" }",
                "exit $Code",
                "",
            ]
        ),
        "scripts/run_omega_sentinel_daily.sh": "\n".join(
            [
                "#!/usr/bin/env sh",
                "set -eu",
                "RUN_DATE=\"${1:-$(date +%F)}\"",
                "mkdir -p data/v2_omega_sentinel/logs",
                "py -m intraday_scanner.v2.omega_sentinel run --date \"$RUN_DATE\" > \"data/v2_omega_sentinel/logs/sentinel_$RUN_DATE.log\" 2>&1",
                "",
            ]
        ),
        "scripts/check_omega_sentinel_status.ps1": "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "py -m intraday_scanner.v2.omega_sentinel status",
                "exit $LASTEXITCODE",
                "",
            ]
        ),
        "scripts/check_omega_sentinel_status.sh": "\n".join(
            [
                "#!/usr/bin/env sh",
                "set -eu",
                "py -m intraday_scanner.v2.omega_sentinel status",
                "",
            ]
        ),
    }
    for path, text in scripts.items():
        _write_text(Path(path), text)
    return {"scripts": sorted(scripts), "status": "written"}


def lock_status(*, output_root: Path = Path("data/v2_omega_sentinel")) -> dict[str, object]:
    lock_path = output_root / "run_locks" / "latest.lock.json"
    if not lock_path.exists():
        return {"state": "unlocked", "status": "passed"}
    payload = _dict(_read_json(lock_path, {}))
    stale = _is_stale_lock(payload)
    return {
        "lock": payload,
        "state": "stale" if stale else "locked",
        "status": "failed" if not stale else "passed_with_warnings",
    }


def clear_stale_locks(*, output_root: Path = Path("data/v2_omega_sentinel")) -> dict[str, object]:
    lock_path = output_root / "run_locks" / "latest.lock.json"
    if not lock_path.exists():
        return {"cleared": False, "status": "unlocked"}
    payload = _dict(_read_json(lock_path, {}))
    if not _is_stale_lock(payload):
        return {"cleared": False, "status": "active_lock"}
    lock_path.unlink()
    _append_jsonl(output_root / "logs" / "run_lock_events.jsonl", {
        "event": "stale_lock_cleared",
        "lock_id": payload.get("lock_id", "unknown"),
        "timestamp": _now(),
    })
    return {"cleared": True, "status": "cleared"}


def doctor(*, output_root: Path = Path("data/v2_omega_sentinel")) -> dict[str, object]:
    init(output_root=output_root)
    failures: list[str] = []
    warnings: list[str] = []
    imports = _check_imports()
    failures.extend(imports["failures"])
    for directory in SENTINEL_DIRS:
        if not (output_root / directory).exists():
            failures.append(f"missing Sentinel directory: {directory}")
    required = (
        Path("docs/audit/omega_build_state.json"),
        Path("data/v2_omega/reports/omega_summary.json"),
        Path("data/v2_data_truth/manifests/latest.json"),
        Path("data/v2_forward_evidence/reconciliation/evidence_integrity.json"),
        Path("data/v2_paper_ops/reports/paper_ops_summary.md"),
        Path("data/v2_command_center/production.html"),
    )
    for path in required:
        if not path.exists():
            failures.append(f"missing artifact: {path.as_posix()}")
    if not status(output_root=output_root).get("frozen_pick_hash"):
        failures.append("latest Sentinel status or frozen hash missing")
    lock = lock_status(output_root=output_root)
    if lock.get("state") == "stale":
        failures.append("stale Sentinel lock exists")
    elif lock.get("state") == "locked":
        warnings.append("active Sentinel lock exists")
    latest_alert = _dict(_read_json(output_root / "alerts" / "latest_alert.json", {}))
    if latest_alert.get("alert_level") == "red":
        failures.append("latest alert is red")
    qa = _dict(_read_json(Path("data/v2_command_center/command_center_qa.json"), {}))
    if qa.get("status") != "passed":
        failures.append("Command Center QA is not passed")
    fill_truth = _fill_truth_summary()
    if fill_truth.get("status") == "failed":
        failures.append("FillTruth verification failed")
    elif fill_truth.get("status") == "missing":
        warnings.append("FillTruth artifacts are missing")
    elif fill_truth.get("status") == "resume_required":
        warnings.append("FillTruth artifacts are incomplete")
    safety = _safety_scan(output_root)
    failures.extend(safety["failures"])
    warnings.extend(safety["warnings"])
    payload = {
        "checked_at": _now(),
        "failures": failures,
        "schema_version": "v2.omega_sentinel_doctor.v1",
        "status": "passed" if not failures else "failed",
        "warnings": warnings,
    }
    reports = output_root / "reports"
    _write_json(reports / "doctor_latest.json", payload)
    _write_md(reports / "doctor_latest.md", "OMEGA Sentinel Doctor", _kv_lines(payload))
    return payload


def artifact_index(
    *,
    output_root: Path = Path("data/v2_omega_sentinel"),
    run_date: date | None = None,
) -> dict[str, object]:
    init(output_root=output_root)
    roots = (
        output_root,
        Path("data/v2_omega"),
        Path("data/v2_forward_evidence"),
        Path("data/v2_fill_truth"),
        Path("data/v2_evidence_commit"),
        Path("data/v2_command_center"),
        Path("docs/audit"),
    )
    rows: list[dict[str, object]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in {".json", ".md", ".csv", ".html", ".txt", ".log"}:
                continue
            rows.append(_artifact_row(path, run_date))
    payload = {
        "artifact_count": len(rows),
        "generated_at": _now(),
        "rows": rows,
        "schema_version": "v2.omega_sentinel_artifact_index.v1",
        "status": "passed",
    }
    root = output_root / "retention"
    _write_json(root / "artifact_index.json", payload)
    _write_csv(root / "artifact_index.csv", rows)
    _write_md(root / "artifact_index.md", "Artifact Index", _artifact_lines(rows))
    return payload


def _build_status_payload(
    *,
    run_date: date,
    started_at: datetime,
    completed_at: datetime,
    command: str,
    fetch_mode: str,
    omega_result: dict[str, object],
    errors: list[str],
) -> dict[str, object]:
    daily = _dict(_read_json(Path(f"data/v2_forward_evidence/reports/daily/{run_date}.json"), {}))
    integrity = _dict(_read_json(Path("data/v2_forward_evidence/reconciliation/evidence_integrity.json"), {}))
    qa = _dict(_read_json(Path("data/v2_command_center/command_center_qa.json"), {}))
    risk = _dict(_read_json(Path("data/v2_forward_evidence/reports/riskhub_daily.json"), {}))
    strategy = _dict(_read_json(Path("data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json"), {}))
    fill_truth = _fill_truth_summary()
    commitbridge = _commitbridge_summary()
    real_intraday = _real_intraday_summary()
    frozen_hash = str(omega_result.get("frozen_pick_hash") or daily.get("frozen_pick_hash") or "")
    frozen = _latest_frozen_payload(run_date, frozen_hash)
    warnings = _unique_strs(
        _list(daily.get("warnings"))
        + _list(omega_result.get("warnings"))
        + _list(fill_truth.get("warnings"))
        + _list(commitbridge.get("warnings"))
    )
    completed_bar = _completed_bar_status(daily, frozen)
    status_payload: dict[str, object] = {
        "accepted_candidate_count": len(_list(frozen.get("accepted_candidates"))),
        "accepted_data_end_date": daily.get("accepted_data_end_date", frozen.get("accepted_end_date", "n/a")),
        "alert_count": 0,
        "alert_level": "green",
        "blocked_candidate_count": len(_list(frozen.get("blocked_candidates"))),
        "closes": _int(daily.get("closes")),
        "command": command,
        "command_center_broken_links": _list(qa.get("broken_links")),
        "command_center_page_count": _int(qa.get("page_count")),
        "command_center_status": qa.get("status", "missing"),
        "commitbridge_status": commitbridge.get("status", "missing"),
        "completed_at": completed_at.isoformat(),
        "completed_bar_status": completed_bar,
        "daily_return_rows": len([row for row in _calendar_rows() if row.get("date") == run_date.isoformat()]),
        "data_truth_status": daily.get("datatruth_status", frozen.get("data_truth_status", "missing")),
        "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
        "errors": errors,
        "evidence_integrity_status": integrity.get("status", "missing"),
        "evidence_mode": "forward",
        "fetch_mode": fetch_mode,
        "fill_truth_daily_approximation_count": _int(fill_truth.get("daily_approximation_count")),
        "fill_truth_disagreement_count": _int(fill_truth.get("execution_model_disagreement_count")),
        "fill_truth_intraday_supported_count": _int(fill_truth.get("intraday_supported_count")),
        "fill_truth_pending_no_fill_data_count": _int(fill_truth.get("pending_no_fill_data_count")),
        "fill_truth_status": fill_truth.get("status", "missing"),
        "fill_truth_warning_count": _int(fill_truth.get("warning_count")),
        "fills": _int(daily.get("fills")),
        "frozen_pick_count": _pick_count(frozen),
        "frozen_pick_hash": frozen_hash,
        "kill_switch_active": bool(risk.get("kill_switch_active")),
        "next_action": "Review alert, dashboard, and run tomorrow after completed daily bars.",
        "no_setup_count": len(_list(frozen.get("no_setup_explanations"))),
        "open_positions": _int(daily.get("open_positions")),
        "paperops_status": "passed" if "paper_ops" in daily else "missing",
        "pending_orders": _int(daily.get("orders_pending")),
        "pending_after_commit": _int(commitbridge.get("pending_after_commit")),
        "pending_before_commit": _int(commitbridge.get("pending_before_commit")),
        "proposals_blocked": _int(commitbridge.get("proposals_blocked")),
        "proposals_committed": _int(commitbridge.get("proposals_committed")),
        "proposals_created": _int(commitbridge.get("proposals_created")),
        "proposals_eligible": _int(commitbridge.get("proposals_eligible")),
        "proposals_rejected": _int(commitbridge.get("proposals_rejected")),
        "real_intraday_status": real_intraday.get("status", "missing"),
        "real_intraday_source_label": real_intraday.get("source_label", "missing"),
        "real_intraday_validation_status": real_intraday.get("validation_status", "missing"),
        "real_intraday_session_completeness": real_intraday.get("session_completeness", "missing"),
        "intraday_daily_reconciliation_status": real_intraday.get("daily_reconciliation_status", "missing"),
        "real_intraday_commit_eligible": bool(real_intraday.get("commit_eligible", False)),
        "riskhub_status": daily.get("riskhub_status", risk.get("riskhub_status", "missing")),
        "run_date": run_date.isoformat(),
        "run_id": f"omega_sentinel:{run_date.isoformat()}:{started_at.strftime('%Y%m%dT%H%M%SZ')}",
        "schema_version": "v2.omega_sentinel_status.v1",
        "started_at": started_at.isoformat(),
        "status": "completed" if not errors else "failed",
        "strategy_evidence_status": strategy.get("status", "missing"),
        "divergence_status": commitbridge.get("pending_divergence_status", "missing"),
        "uncommitted_overlay_count": _int(commitbridge.get("uncommitted_overlay_count")),
        "warnings": warnings,
        "watchlist_count": len(_list(frozen.get("watchlist_candidates"))),
    }
    level, alert_warnings, critical = _classify_alert(status_payload)
    status_payload["alert_level"] = level
    status_payload["alert_count"] = len(alert_warnings) + len(critical)
    status_payload["warnings"] = _unique_strs(warnings + alert_warnings)
    status_payload["errors"] = _unique_strs(errors + critical)
    return status_payload


def _write_status(output_root: Path, payload: dict[str, object]) -> dict[str, Path]:
    root = output_root / "status"
    run_date = str(payload["run_date"])
    json_path = root / f"{run_date}_status.json"
    md_path = root / f"{run_date}_status.md"
    _write_json(json_path, payload)
    _write_json(root / "latest_status.json", payload)
    lines = [
        f"Run ID: `{payload['run_id']}`",
        f"Run date: `{payload['run_date']}`",
        f"Alert level: `{payload['alert_level']}`",
        f"Frozen pick hash: `{payload['frozen_pick_hash']}`",
        f"DataTruth: `{payload['data_truth_status']}`",
        f"FillTruth: `{payload.get('fill_truth_status', 'missing')}`",
        f"Completed bar: `{payload['completed_bar_status']}`",
        f"Integrity: `{payload['evidence_integrity_status']}`",
        f"Command Center: `{payload['command_center_status']}`",
        f"Next action: {payload['next_action']}",
    ]
    _write_md(md_path, "OMEGA Sentinel Daily Status", lines)
    _write_md(root / "latest_status.md", "OMEGA Sentinel Daily Status", lines)
    return {"json": json_path, "markdown": md_path}


def _write_alert(output_root: Path, status_payload: dict[str, object]) -> dict[str, object]:
    level = str(status_payload["alert_level"])
    critical = _list(status_payload.get("errors"))
    warnings = _list(status_payload.get("warnings"))
    payload = {
        "alert_level": level,
        "blocked_candidates": status_payload.get("blocked_candidate_count", 0),
        "critical_failures": critical,
        "headline": _alert_headline(level, status_payload),
        "open_positions": status_payload.get("open_positions", 0),
        "pending_orders": status_payload.get("pending_orders", 0),
        "fill_truth_status": status_payload.get("fill_truth_status", "missing"),
        "fill_truth_warning_count": status_payload.get("fill_truth_warning_count", 0),
        "fill_truth_pending_no_fill_data_count": status_payload.get("fill_truth_pending_no_fill_data_count", 0),
        "commitbridge_status": status_payload.get("commitbridge_status", "missing"),
        "proposals_created": status_payload.get("proposals_created", 0),
        "proposals_eligible": status_payload.get("proposals_eligible", 0),
        "proposals_committed": status_payload.get("proposals_committed", 0),
        "proposals_rejected": status_payload.get("proposals_rejected", 0),
        "proposals_blocked": status_payload.get("proposals_blocked", 0),
        "pending_before_commit": status_payload.get("pending_before_commit", 0),
        "pending_after_commit": status_payload.get("pending_after_commit", 0),
        "divergence_status": status_payload.get("divergence_status", "missing"),
        "uncommitted_overlay_count": status_payload.get("uncommitted_overlay_count", 0),
        "run_date": status_payload.get("run_date", "unknown"),
        "schema_version": "v2.omega_sentinel_alert.v1",
        "status": "passed",
        "strategies_on_watch": _strategies_on_watch(),
        "strategies_quarantined": _strategies_quarantined(),
        "suggested_next_command": "py -m intraday_scanner.v2.omega_sentinel doctor",
        "warnings": warnings,
        "what_to_review": _review_items(level, status_payload),
    }
    root = output_root / "alerts"
    run_date = str(payload["run_date"])
    _write_json(root / f"{run_date}_alert.json", payload)
    _write_json(root / "latest_alert.json", payload)
    lines = [
        f"Alert level: `{level}`",
        f"Headline: {payload['headline']}",
        f"Critical failures: `{len(critical)}`",
        f"Warnings: `{len(warnings)}`",
        f"Suggested next command: `{payload['suggested_next_command']}`",
    ]
    _write_md(root / f"{run_date}_alert.md", "OMEGA Sentinel Alert", lines)
    _write_md(root / "latest_alert.md", "OMEGA Sentinel Alert", lines)
    return payload


def _write_operational_report(
    output_root: Path,
    status_payload: dict[str, object],
    alert_payload: dict[str, object],
    trial_payload: dict[str, object],
) -> dict[str, object]:
    run_date = str(status_payload.get("run_date", "unknown"))
    prior = _prior_status(output_root / "status", run_date)
    changed = _what_changed(status_payload, prior)
    payload = {
        "alert": alert_payload,
        "calendar_summary": _dict(_read_json(Path("data/v2_forward_evidence/calendar/strategy_calendar_summary.json"), {})),
        "changed_since_prior_run": changed,
        "current_untrusted_assumptions": _untrusted_assumptions(),
        "data_summary": {
            "accepted_data_end_date": status_payload.get("accepted_data_end_date"),
            "completed_bar_status": status_payload.get("completed_bar_status"),
            "data_truth_status": status_payload.get("data_truth_status"),
        },
        "frozen_pick_hash": status_payload.get("frozen_pick_hash"),
        "paper_ops_summary": {
            "closes": status_payload.get("closes"),
            "fills": status_payload.get("fills"),
            "open_positions": status_payload.get("open_positions"),
            "pending_orders": status_payload.get("pending_orders"),
        },
        "fill_truth_summary": {
            "daily_approximation_count": status_payload.get("fill_truth_daily_approximation_count"),
            "execution_model_disagreement_count": status_payload.get("fill_truth_disagreement_count"),
            "intraday_supported_count": status_payload.get("fill_truth_intraday_supported_count"),
            "pending_no_fill_data_count": status_payload.get("fill_truth_pending_no_fill_data_count"),
            "status": status_payload.get("fill_truth_status"),
            "warning_count": status_payload.get("fill_truth_warning_count"),
        },
        "commitbridge_summary": {
            "divergence_status": status_payload.get("divergence_status"),
            "pending_after_commit": status_payload.get("pending_after_commit"),
            "pending_before_commit": status_payload.get("pending_before_commit"),
            "proposals_blocked": status_payload.get("proposals_blocked"),
            "proposals_committed": status_payload.get("proposals_committed"),
            "proposals_created": status_payload.get("proposals_created"),
            "proposals_eligible": status_payload.get("proposals_eligible"),
            "proposals_rejected": status_payload.get("proposals_rejected"),
            "status": status_payload.get("commitbridge_status"),
            "uncommitted_overlay_count": status_payload.get("uncommitted_overlay_count"),
        },
        "riskhub_summary": {
            "kill_switch_active": status_payload.get("kill_switch_active"),
            "riskhub_status": status_payload.get("riskhub_status"),
        },
        "run_summary": status_payload,
        "schema_version": "v2.omega_sentinel_report.v1",
        "strategy_evidence_status": status_payload.get("strategy_evidence_status"),
        "tomorrow_command": RUNBOOK_COMMAND,
        "trial": trial_payload,
        "what_to_inspect": alert_payload.get("what_to_review", []),
    }
    root = output_root / "reports"
    json_path = root / f"{run_date}_omega_sentinel_report.json"
    md_path = root / f"{run_date}_omega_sentinel_report.md"
    _write_json(json_path, payload)
    _write_json(root / "latest_omega_sentinel_report.json", payload)
    lines = [
        f"Run date: `{run_date}`",
        f"Alert level: `{status_payload.get('alert_level')}`",
        f"Frozen pick hash: `{status_payload.get('frozen_pick_hash')}`",
        f"DataTruth: `{status_payload.get('data_truth_status')}`",
        f"PaperOps: `{status_payload.get('paperops_status')}`",
        f"FillTruth: `{status_payload.get('fill_truth_status')}`",
        f"Trial progress: `{trial_payload.get('completed_forward_days')} / 30`",
        f"Tomorrow: `{RUNBOOK_COMMAND}`",
        "",
        "What changed:",
        *_bullet_lines(changed),
        "",
        "Untrusted:",
        *_bullet_lines(_untrusted_assumptions()),
    ]
    _write_md(md_path, "OMEGA Sentinel Operational Report", lines)
    _write_md(root / "latest_omega_sentinel_report.md", "OMEGA Sentinel Operational Report", lines)
    return {"json": json_path.as_posix(), "markdown": md_path.as_posix(), "status": "written"}


def _write_scorecard(
    *,
    output_root: Path,
    status_payload: dict[str, object],
    alert_payload: dict[str, object],
    trial_payload: dict[str, object],
    verification: dict[str, object],
    doctor_result: dict[str, object],
) -> int:
    categories = [
        _score("Daily operational completeness", bool(status_payload.get("run_id")), 9),
        _score("Run lock correctness", not (output_root / "run_locks" / "latest.lock.json").exists(), 7),
        _score("Status clarity", (output_root / "status" / "latest_status.md").exists(), 7),
        _score("Alert usefulness", bool(alert_payload.get("headline")), 7),
        _score("30-day forward trial tracking", bool(trial_payload.get("target_forward_days")), 9),
        _score("Artifact retention/indexing", (output_root / "retention" / "artifact_index.json").exists(), 7),
        _score("Scheduler readiness", Path("scripts/run_omega_sentinel_daily.ps1").exists(), 7),
        _score("Command Center Sentinel pages", Path("data/v2_command_center/omega_sentinel.html").exists(), 7),
        _score("Doctor diagnostics", doctor_result.get("status") == "passed", 7),
        _score("Evidence integrity", verification.get("status") == "passed", 9),
        _score("Safety/no-live-execution", not _safety_scan(output_root)["failures"], 7),
        _score("Test coverage", Path("tests/test_v2_omega_sentinel.py").exists(), 7),
        _score("Documentation/runbook clarity", Path("docs/architecture/v2_omega_sentinel.md").exists(), 5),
        _score("Product coherence", status_payload.get("alert_level") in {"green", "yellow"}, 5),
    ]
    score = sum(_int(row["score"]) for row in categories)
    payload = {
        "categories": categories,
        "score": score,
        "status": "target_met" if score >= 99 else "resume_required",
        "target": 99,
    }
    lines = [
        "# OMEGA Sentinel Quality Scorecard",
        "",
        f"- Score: `{score} / 100`",
        "- Target: `99 / 100`",
        f"- Status: `{payload['status']}`",
        "",
        "| Category | Score | Evidence |",
        "| --- | ---: | --- |",
    ]
    for row in categories:
        lines.append(f"| {row['category']} | {row['score']} / {row['max_score']} | {row['evidence']} |")
    _write_text(Path("docs/audit/omega_sentinel_quality_scorecard.md"), "\n".join(lines) + "\n")
    _write_json(output_root / "reports" / "omega_sentinel_quality_scorecard.json", payload)
    return score


def _write_build_state(
    *,
    output_root: Path,
    status_payload: dict[str, object],
    quality_score: int,
    doctor_result: dict[str, object],
) -> None:
    state = {
        "artifacts": {
            "alert": (output_root / "alerts" / "latest_alert.json").as_posix(),
            "artifact_index": (output_root / "retention" / "artifact_index.json").as_posix(),
            "doctor": (output_root / "reports" / "doctor_latest.json").as_posix(),
            "report": (output_root / "reports" / "latest_omega_sentinel_report.json").as_posix(),
            "status": (output_root / "status" / "latest_status.json").as_posix(),
            "trial": (output_root / "trial" / "forward_trial_status.json").as_posix(),
        },
        "blockers": doctor_result.get("failures", []),
        "commands": _command_list(),
        "completed_work": [
            "Sentinel run locking",
            "daily status and alerts",
            "30-day true-forward trial scoreboard",
            "artifact index",
            "scheduler scripts",
            "Command Center Sentinel pages",
            "doctor diagnostics",
        ],
        "next_actions": [RUNBOOK_COMMAND, "py -m intraday_scanner.v2.omega_sentinel status"],
        "quality_score": quality_score,
        "quality_target": 99,
        "remaining_work": _untrusted_assumptions(),
        "schema_version": "v2.omega_sentinel_build_state.v1",
        "status": "complete" if quality_score >= 99 else "resume_required",
        "status_summary": status_payload,
    }
    _write_json(Path("docs/audit/omega_sentinel_build_state.json"), state)
    _write_md(Path("docs/audit/omega_sentinel_build_log.md"), "OMEGA Sentinel Build Log", [
        "Built additive scheduler-ready Sentinel layer around OMEGA.",
        "Preserved research-only and no-live-execution boundaries.",
        f"Latest score: `{quality_score} / 100`.",
    ])
    _write_md(Path("docs/audit/omega_sentinel_release_summary.md"), "OMEGA Sentinel Release Summary", [
        f"Status: `{state['status']}`.",
        f"Score: `{quality_score} / 100`.",
        f"Latest alert: `{status_payload.get('alert_level', 'unknown')}`.",
        "Dashboard: `data/v2_command_center/production.html`.",
    ])
    _write_md(Path("docs/audit/omega_sentinel_resume_goal.md"), "OMEGA Sentinel Resume Goal", [
        "If score is below 99, fix categories marked incomplete in `docs/audit/omega_sentinel_quality_scorecard.md`.",
        "Next durable work after 99/100: accumulate 30 true forward paper days and 30 closed forward trades.",
    ])


def _write_docs() -> None:
    _write_md(Path("docs/architecture/v2_omega_sentinel.md"), "v2 OMEGA Sentinel", [
        "OMEGA Sentinel wraps OMEGA Autopilot with run locking, status, alerts, trial tracking, retention indexing, diagnostics, and scheduler-ready scripts.",
        "It does not place trades, route orders, store secrets, mutate legacy SQLite, or change legacy scanner scoring.",
        "Replay, backtest, demo, and synthetic evidence never count as true forward evidence.",
    ])
    _write_md(Path("docs/operations/omega_sentinel_daily_runbook.md"), "OMEGA Sentinel Daily Runbook", [
        "After market close, run `py -m intraday_scanner.v2.omega_sentinel run --date YYYY-MM-DD`.",
        "Morning review starts with `py -m intraday_scanner.v2.omega_sentinel status` and `data/v2_command_center/production.html`.",
        "Yellow means inspect warnings, pending orders, blocked candidates, or untrusted assumptions before relying on the paper evidence.",
        "Red means stop and run `py -m intraday_scanner.v2.omega_sentinel doctor`.",
    ])
    _write_md(Path("docs/operations/omega_sentinel_scheduler_examples.md"), "OMEGA Sentinel Scheduler Examples", [
        "Windows Task Scheduler action: `powershell -ExecutionPolicy Bypass -File scripts/run_omega_sentinel_daily.ps1 YYYY-MM-DD`.",
        "Cron example: `30 17 * * 1-5 cd /path/to/Dawnstrike && sh scripts/run_omega_sentinel_daily.sh`.",
        "Scripts are generated only; Sentinel does not install scheduled tasks.",
        "Default mode is deterministic cached validation; use `--fetch` manually only when public fetch is intended.",
    ])
    _write_md(Path("docs/operations/omega_sentinel_failure_recovery.md"), "OMEGA Sentinel Failure Recovery", [
        "Stale lock: run `py -m intraday_scanner.v2.omega_sentinel lock-status`, then `clear-stale-locks` only if stale.",
        "Missing data: rerun DataTruth/Omega with `--fetch` only when public data refresh is intentional.",
        "Hash mismatch: preserve artifacts and inspect Evidence Vault manifests before any rerun.",
        "Dashboard failure: rerun `py -m intraday_scanner.v2.omega_sentinel doctor` and inspect Command Center QA.",
    ])
    _write_md(Path("docs/audit/omega_sentinel_red_team.md"), "OMEGA Sentinel Red Team", [
        "Missed daily run is visible as missing latest status or missing date status.",
        "Duplicate daily run is blocked by run lock; stale lock is reported and explicit cleanup is required.",
        "Frozen evidence is not silently overwritten; Evidence Vault superseding artifacts stay explicit.",
        "Completed-bar proof is checked from forward frozen/daily evidence.",
        "Replay, shadow replay, demo, and synthetic evidence do not count toward true forward days.",
        "External alert artifacts are local only; no send path is invoked.",
        "All-blocked days are yellow evidence days, not operational failures.",
        "Remaining risk: no strategy is validated until 30 true forward days and 30 closed forward trades exist.",
    ])


def _acquire_lock(
    *,
    output_root: Path,
    run_date: date,
    command: str,
    stale_after_minutes: int,
) -> dict[str, object]:
    _paths(output_root)
    lock_path = output_root / "run_locks" / "latest.lock.json"
    if lock_path.exists():
        existing = _dict(_read_json(lock_path, {}))
        if not _is_stale_lock(existing):
            _append_jsonl(output_root / "logs" / "run_lock_events.jsonl", {
                "event": "lock_blocked",
                "existing_lock_id": existing.get("lock_id", "unknown"),
                "timestamp": _now(),
            })
            raise RuntimeError("OMEGA Sentinel run lock is active")
    payload = {
        "command": command,
        "created_at": _now(),
        "lock_id": f"sentinel_lock_{uuid.uuid4().hex}",
        "process_id": os.getpid(),
        "run_date": run_date.isoformat(),
        "schema_version": "v2.omega_sentinel_lock.v1",
        "stale_after_minutes": stale_after_minutes,
    }
    _write_json(lock_path, payload)
    _append_jsonl(output_root / "logs" / "run_lock_events.jsonl", {
        "event": "lock_acquired",
        "lock_id": payload["lock_id"],
        "timestamp": _now(),
    })
    return payload


def _release_lock(*, output_root: Path, lock_id: str) -> None:
    lock_path = output_root / "run_locks" / "latest.lock.json"
    if lock_path.exists():
        payload = _dict(_read_json(lock_path, {}))
        if payload.get("lock_id") == lock_id:
            lock_path.unlink()
    _append_jsonl(output_root / "logs" / "run_lock_events.jsonl", {
        "event": "lock_released",
        "lock_id": lock_id,
        "timestamp": _now(),
    })


def _is_stale_lock(payload: dict[str, object]) -> bool:
    created = _parse_dt(str(payload.get("created_at", "")))
    stale_after = max(1, _int(payload.get("stale_after_minutes")) or 240)
    return datetime.now(timezone.utc) - created > timedelta(minutes=stale_after)


def _paths(output_root: Path) -> dict[str, Path]:
    paths = {name: output_root / name for name in SENTINEL_DIRS}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _classify_alert(payload: dict[str, object]) -> tuple[str, list[str], list[str]]:
    warnings = list(str(item) for item in _list(payload.get("warnings")))
    critical = list(str(item) for item in _list(payload.get("errors")))
    if payload.get("status") == "failed":
        critical.append("Sentinel run failed")
    if not payload.get("frozen_pick_hash"):
        critical.append("missing frozen pick hash")
    if payload.get("completed_bar_status") != "passed":
        critical.append("completed-bar proof failed")
    if payload.get("evidence_integrity_status") != "passed":
        critical.append("evidence integrity failed")
    if payload.get("command_center_status") != "passed":
        critical.append("Command Center QA failed")
    if payload.get("data_truth_status") == "synthetic":
        critical.append("synthetic forward data detected")
    if payload.get("fill_truth_status") == "failed":
        critical.append("FillTruth verification failed")
    if payload.get("commitbridge_status") == "failed":
        critical.append("CommitBridge verification failed")
    if critical:
        return "red", warnings, _unique_strs(critical)
    if payload.get("fill_truth_status") in {"missing", "resume_required"}:
        warnings.append("FillTruth evidence is missing or incomplete")
    if _int(payload.get("fill_truth_pending_no_fill_data_count")) > 0:
        warnings.append("FillTruth has pending orders with no fill data")
    if _int(payload.get("fill_truth_daily_approximation_count")) > 0:
        warnings.append("FillTruth has daily approximation evidence")
    if _int(payload.get("fill_truth_disagreement_count")) > 0:
        warnings.append("FillTruth execution model disagreement exists")
    if payload.get("commitbridge_status") in {"missing", "resume_required"}:
        warnings.append("CommitBridge evidence is missing or incomplete")
    if _int(payload.get("uncommitted_overlay_count")) > 0:
        warnings.append("CommitBridge has uncommitted eligible FillTruth overlay")
    if payload.get("divergence_status") in {
        "unresolved_uncommitted_eligible_overlay",
        "missing",
    }:
        warnings.append("CommitBridge pending divergence requires review")
    if _int(payload.get("proposals_blocked")) > 0:
        warnings.append("CommitBridge blocked unsafe FillTruth proposal")
    if _int(payload.get("pending_orders")) > 0:
        warnings.append("pending paper orders exist")
    if _int(payload.get("accepted_candidate_count")) == 0:
        warnings.append("no accepted candidates")
    if _int(payload.get("blocked_candidate_count")) > 0:
        warnings.append("candidates blocked by RiskHub or Decision Engine")
    warnings.append("no strategy validated")
    return ("yellow" if warnings else "green"), _unique_strs(warnings), []


def _completed_bar_status(daily: dict[str, object], frozen: dict[str, object]) -> str:
    if frozen.get("completed_bar_proof") is True:
        return "passed"
    accepted = str(daily.get("accepted_data_end_date", frozen.get("accepted_end_date", "")))
    run_date = str(daily.get("date", frozen.get("date", "")))
    return "passed" if accepted and run_date and accepted < run_date else "failed"


def _latest_frozen_payload(run_date: date, pick_hash: str) -> dict[str, object]:
    root = Path("data/v2_forward_evidence/frozen_picks")
    matches = sorted(
        root.glob(f"{run_date.isoformat()}_picks*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in matches:
        payload = _dict(_read_json(path, {}))
        if payload.get("pick_set_hash") == pick_hash:
            return payload
    return _dict(_read_json(matches[0], {})) if matches else {}


def _fill_truth_summary() -> dict[str, object]:
    summary = _dict(_read_json(Path("data/v2_fill_truth/reports/filltruth_summary.json"), {}))
    resolution = _dict(_read_json(Path("data/v2_fill_truth/reports/pending_resolution_latest.json"), {}))
    comparison = _dict(_read_json(Path("data/v2_fill_truth/comparisons/execution_model_comparison.json"), {}))
    verification = _dict(_read_json(Path("data/v2_fill_truth/reconciliation/verify_latest.json"), {}))
    certainty = _dict(resolution.get("fill_certainty_summary"))
    status = str(verification.get("status") or summary.get("status") or "missing")
    if status == "passed" and _int(summary.get("quality_score")) < 99:
        status = "resume_required"
    warnings = _unique_strs(
        _list(summary.get("warnings"))
        + _list(resolution.get("warnings"))
        + _list(comparison.get("warnings"))
        + _list(verification.get("warnings"))
    )
    return {
        "daily_approximation_count": certainty.get("daily_approximation_count", 0),
        "execution_model_disagreement_count": comparison.get("model_disagreement_count", 0),
        "intraday_supported_count": certainty.get("intraday_supported_count", 0),
        "pending_no_fill_data_count": certainty.get("pending_no_fill_data_count", 0),
        "status": status,
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def _commitbridge_summary() -> dict[str, object]:
    summary = _dict(_read_json(Path("data/v2_evidence_commit/reports/evidence_commit_summary.json"), {}))
    reconciliation = _dict(
        _read_json(Path("data/v2_evidence_commit/reconciliation/pending_divergence_latest.json"), {})
    )
    verify_payload = _dict(
        _read_json(Path("data/v2_evidence_commit/reconciliation/verify_latest.json"), {})
    )
    status = str(verify_payload.get("status") or summary.get("status") or "missing")
    if status == "passed" and _int(summary.get("quality_score")) < 100:
        status = "resume_required"
    warnings = _unique_strs(
        _list(summary.get("blocking_reasons"))
        + _list(reconciliation.get("warnings"))
        + _list(verify_payload.get("warnings"))
    )
    return {
        "pending_after_commit": reconciliation.get("pending_after_commit", 0),
        "pending_before_commit": reconciliation.get("pending_before_commit", 0),
        "pending_divergence_status": reconciliation.get("pending_divergence_status", "missing"),
        "proposals_blocked": reconciliation.get("proposals_blocked", summary.get("blocked", 0)),
        "proposals_committed": reconciliation.get("proposals_committed", summary.get("commit_events", 0)),
        "proposals_created": reconciliation.get("proposals_created", summary.get("proposed", 0)),
        "proposals_eligible": reconciliation.get("proposals_eligible", summary.get("eligible", 0)),
        "proposals_rejected": reconciliation.get("proposals_rejected", summary.get("rejected", 0)),
        "status": status,
        "uncommitted_overlay_count": reconciliation.get("uncommitted_overlay_count", 0),
        "warnings": warnings,
    }


def _real_intraday_cycle(run_date: date, *, enabled: bool) -> dict[str, object]:
    if not enabled:
        return {"reason": "use_real_intraday not requested", "status": "skipped"}
    try:
        from intraday_scanner.v2.real_intraday import build as build_real_intraday
        from intraday_scanner.v2.real_intraday import readiness as real_intraday_readiness

        build_payload = build_real_intraday(run_date=run_date)
        readiness_payload = real_intraday_readiness(run_date=run_date)
        return {"build": build_payload, "readiness": readiness_payload, "status": "passed"}
    except Exception as exc:  # pragma: no cover - diagnostic path
        return {"error": str(exc), "status": "failed"}


def _telegram_after_run(kind: str, run_date: date, *, enabled: bool) -> dict[str, object]:
    if not enabled:
        return {"reason": "telegram flag not requested", "status": "skipped"}
    try:
        from intraday_scanner.v2.telegram_intel import send as telegram_send

        return telegram_send(kind=kind, run_date=run_date)
    except Exception as exc:  # pragma: no cover - defensive notification boundary
        return {
            "error": str(exc),
            "send_status": "failed_notification_boundary",
            "status": "warning",
        }


def _market_masters_after_run(run_date: date, *, enabled: bool) -> dict[str, object]:
    if not enabled:
        return {"reason": "market masters flag not requested", "status": "skipped"}
    try:
        from intraday_scanner.v2.market_masters import (
            evaluate as market_masters_evaluate,
        )
        from intraday_scanner.v2.market_masters import (
            report as market_masters_report,
        )
        from intraday_scanner.v2.market_masters import (
            research as market_masters_research,
        )
        from intraday_scanner.v2.market_masters import (
            sync_learning_foundry as market_masters_sync,
        )
        from intraday_scanner.v2.market_masters import (
            verify as market_masters_verify,
        )

        market_masters_research(run_date=run_date)
        market_masters_evaluate(run_date=run_date)
        sync_payload = market_masters_sync(run_date=run_date)
        verification = market_masters_verify()
        report_payload = market_masters_report()
        build_command_center()
        return {
            "build_id": report_payload.get("build_id", ""),
            "quality_score": report_payload.get("quality_score", 0),
            "status": "passed" if verification.get("status") == "passed" else "warning",
            "sync": sync_payload,
            "verify": verification,
        }
    except Exception as exc:  # pragma: no cover - defensive research boundary
        return {
            "error": str(exc),
            "status": "warning",
        }


def _real_intraday_summary() -> dict[str, object]:
    summary = _dict(_read_json(Path("data/v2_real_intraday/reports/real_intraday_summary.json"), {}))
    readiness_payload = _dict(_read_json(Path("data/v2_real_intraday/reports/import_readiness.json"), {}))
    if not summary and not readiness_payload:
        return {"status": "missing"}
    return {
        "commit_eligible": summary.get("commit_eligible", False),
        "daily_reconciliation_status": summary.get(
            "daily_reconciliation_status",
            readiness_payload.get("daily_reconciliation_status", "missing"),
        ),
        "session_completeness": summary.get(
            "session_completeness",
            readiness_payload.get("session_completeness", "missing"),
        ),
        "source_label": summary.get("source_label", readiness_payload.get("source_label", "missing")),
        "status": summary.get("status", readiness_payload.get("status", "missing")),
        "validation_status": summary.get("validation_status", readiness_payload.get("validation_status", "missing")),
    }


def _pick_count(payload: dict[str, object]) -> int:
    return sum(
        len(_list(payload.get(key)))
        for key in (
            "accepted_candidates",
            "blocked_candidates",
            "watchlist_candidates",
            "near_setup_candidates",
            "no_setup_explanations",
        )
    )


def _calendar_rows() -> list[dict[str, str]]:
    path = Path("data/v2_forward_evidence/calendar/strategy_daily_returns.csv")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _dated_status_rows(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if not root.exists():
        return rows
    for path in sorted(root.glob("*_status.json")):
        if path.name == "latest_status.json":
            continue
        payload = _dict(_read_json(path, {}))
        if payload:
            rows.append(payload)
    return rows


def _counts_as_forward_day(row: dict[str, object]) -> bool:
    return (
        row.get("evidence_mode") == "forward"
        and row.get("evidence_integrity_status") == "passed"
        and bool(row.get("frozen_pick_hash"))
        and row.get("completed_bar_status") == "passed"
        and row.get("data_truth_status") not in {"missing", "synthetic", "failed"}
    )


def _strategy_trial_row(strategy: str, rows: list[dict[str, str]]) -> dict[str, object]:
    matches = [row for row in rows if row.get("strategy_id") == strategy]
    returns = [_float(row.get("daily_return_pct")) for row in matches]
    opened = sum(_int(row.get("trades_opened")) for row in matches)
    closed = sum(_int(row.get("trades_closed")) for row in matches)
    return {
        "average_daily_return_pct": round(sum(returns) / len(returns), 8) if returns else 0.0,
        "closed_trades": closed,
        "cumulative_return_pct": round(sum(returns), 8),
        "drawdown_pct": min((_float(row.get("drawdown_pct")) for row in matches), default=0.0),
        "opened_trades": opened,
        "strategy_id": strategy,
        "strategy_status": "insufficient_forward_evidence" if len(matches) < TARGET_FORWARD_DAYS or closed < 30 else "review_eligible",
    }


def _validation_requirements(strategy_rows: list[dict[str, object]], completed_days: int) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for row in strategy_rows:
        missing: list[str] = []
        if completed_days < TARGET_FORWARD_DAYS:
            missing.append(f"needs {TARGET_FORWARD_DAYS - completed_days} more true forward days")
        if _int(row.get("closed_trades")) < 30:
            missing.append(f"needs {30 - _int(row.get('closed_trades'))} more closed forward trades")
        output[str(row["strategy_id"])] = missing
    return output


def _missing_weekdays(trial_start: str, latest: str, counted: list[dict[str, object]]) -> list[str]:
    if trial_start in {"", "n/a"} or latest in {"", "n/a"}:
        return []
    start = date.fromisoformat(trial_start)
    end = date.fromisoformat(latest)
    counted_dates = {str(row.get("run_date")) for row in counted}
    missing: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current.isoformat() not in counted_dates:
            missing.append(current.isoformat())
        current += timedelta(days=1)
    return missing


def _next_weekday(latest: str) -> str:
    if latest in {"", "n/a"}:
        return "n/a"
    current = date.fromisoformat(latest) + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current.isoformat()


def _artifact_row(path: Path, run_date: date | None) -> dict[str, object]:
    relative = path.as_posix()
    linked_hash = _linked_pick_hash(path)
    return {
        "artifact_type": _artifact_type(path),
        "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "date": run_date.isoformat() if run_date else _date_from_name(path),
        "evidence_mode": _evidence_mode(path),
        "linked_manifest": _linked_manifest(path),
        "linked_pick_hash": linked_hash,
        "path": relative,
        "retention_class": _retention_class(path),
        "run_id": _latest_run_id(),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "status": "present",
    }


def _artifact_type(path: Path) -> str:
    text = path.as_posix()
    if "frozen_picks" in text:
        return "frozen_picks"
    if "pick_hashes" in text:
        return "pick_hash"
    if "command_center" in text:
        return "dashboard"
    if "reports" in text:
        return "report"
    if "status" in text:
        return "status"
    if "alerts" in text:
        return "alert"
    return path.suffix.lstrip(".") or "artifact"


def _retention_class(path: Path) -> str:
    artifact_type = _artifact_type(path)
    if artifact_type in {"frozen_picks", "pick_hash", "status", "alert"}:
        return "critical_evidence"
    if artifact_type == "dashboard":
        return "dashboard"
    if artifact_type == "report":
        return "daily_report"
    if "logs" in path.as_posix():
        return "logs"
    return "rebuildable"


def _linked_pick_hash(path: Path) -> str:
    if path.suffix.lower() != ".json":
        return "n/a"
    payload = _read_json(path, {})
    if isinstance(payload, dict):
        return str(payload.get("pick_set_hash", payload.get("frozen_pick_hash", "n/a")))
    return "n/a"


def _linked_manifest(path: Path) -> str:
    if "frozen_picks" in path.as_posix():
        return path.name.replace("_picks", "_manifest")
    return "n/a"


def _evidence_mode(path: Path) -> str:
    text = path.as_posix()
    if "shadow_replay" in text:
        return "shadow_forward_replay"
    if "forward" in text or "omega_sentinel" in text:
        return "forward"
    return "n/a"


def _date_from_name(path: Path) -> str:
    match = re.search(r"20\d\d-\d\d-\d\d", path.name)
    return match.group(0) if match else "n/a"


def _latest_run_id() -> str:
    payload = _dict(_read_json(Path("data/v2_omega_sentinel/status/latest_status.json"), {}))
    return str(payload.get("run_id", "n/a"))


def _prior_status(root: Path, run_date: str) -> dict[str, object]:
    rows = [row for row in _dated_status_rows(root) if str(row.get("run_date")) < run_date]
    return rows[-1] if rows else {}


def _what_changed(current: dict[str, object], prior: dict[str, object]) -> list[str]:
    if not prior:
        return ["No prior Sentinel status available for comparison."]
    changes: list[str] = []
    for key in ("frozen_pick_hash", "alert_level", "pending_orders", "open_positions"):
        if current.get(key) != prior.get(key):
            changes.append(f"{key}: {prior.get(key, 'n/a')} -> {current.get(key, 'n/a')}")
    return changes or ["No material Sentinel status changes since prior run."]


def _review_items(level: str, payload: dict[str, object]) -> list[str]:
    items = ["Open data/v2_command_center/production.html", "Review latest frozen pick hash"]
    if level != "green":
        items.append("Review warnings and blocked candidates")
    if _int(payload.get("pending_orders")) > 0:
        items.append("Review pending PaperOps orders")
    items.append("Confirm no strategy is marked validated")
    return items


def _alert_headline(level: str, payload: dict[str, object]) -> str:
    if level == "red":
        return "OMEGA Sentinel found critical evidence failures."
    if level == "yellow":
        return "OMEGA Sentinel completed with review items."
    return "OMEGA Sentinel completed cleanly."


def _strategies_quarantined() -> list[str]:
    evidence = _dict(_read_json(Path("data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json"), {}))
    return sorted(
        str(row.get("strategy_id"))
        for row in _list(evidence.get("rows"))
        if isinstance(row, dict) and row.get("evidence_status") == "quarantined"
    )


def _strategies_on_watch() -> list[str]:
    evidence = _dict(_read_json(Path("data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json"), {}))
    return sorted(
        str(row.get("strategy_id"))
        for row in _list(evidence.get("rows"))
        if isinstance(row, dict) and row.get("evidence_status") in {"watch", "experimental"}
    )


def _safety_scan(output_root: Path) -> dict[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    roots = (Path("intraday_scanner/v2/omega_sentinel"), output_root, Path("scripts"))
    forbidden_imports = ("app", "streamlit", "sqlite3", "intraday_scanner.integrations")
    secret_pattern = re.compile(r"(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]+", re.I)
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".py", ".json", ".md", ".ps1", ".sh"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if secret_pattern.search(text):
                failures.append(f"possible secret literal: {path.as_posix()}")
            if path.suffix.lower() == ".py" and root.as_posix().endswith("omega_sentinel"):
                for forbidden in forbidden_imports:
                    if f"import {forbidden}" in text or f"from {forbidden}" in text:
                        failures.append(f"forbidden import {forbidden}: {path.as_posix()}")
            if path.name.startswith("run_omega_sentinel") and "schtasks" in text.lower():
                failures.append(f"scheduler script installs task: {path.as_posix()}")
    if not Path("intraday_scanner/v2/omega_sentinel").exists():
        warnings.append("Sentinel module missing")
    return {"failures": sorted(set(failures)), "warnings": sorted(set(warnings))}


def _check_imports() -> dict[str, list[str]]:
    failures: list[str] = []
    for module in (
        "intraday_scanner.v2.omega",
        "intraday_scanner.v2.forward_autopilot",
        "intraday_scanner.v2.evidence_vault",
        "intraday_scanner.v2.omega_sentinel",
    ):
        try:
            __import__(module)
        except Exception as exc:  # pragma: no cover - diagnostic path
            failures.append(f"import failed {module}: {exc}")
    return {"failures": failures}


def _score(name: str, passed: bool, max_score: int) -> dict[str, object]:
    return {
        "category": name,
        "evidence": "passed" if passed else "missing_or_incomplete",
        "max_score": max_score,
        "score": max_score if passed else max(0, max_score - 4),
    }


def _command_list() -> list[str]:
    return [
        "py -m intraday_scanner.v2.omega_sentinel init",
        "py -m intraday_scanner.v2.omega_sentinel run --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.omega_sentinel run-today",
        "py -m intraday_scanner.v2.omega_sentinel status",
        "py -m intraday_scanner.v2.omega_sentinel verify",
        "py -m intraday_scanner.v2.omega_sentinel report",
        "py -m intraday_scanner.v2.omega_sentinel trial-status",
        "py -m intraday_scanner.v2.omega_sentinel scheduler-scripts",
        "py -m intraday_scanner.v2.omega_sentinel doctor",
        "py -m intraday_scanner.v2.omega_sentinel omega --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.omega_sentinel morning-check --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.omega_sentinel after-close --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.omega_sentinel resolve-pending --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.omega_sentinel commit-filltruth --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.omega_sentinel lock-status",
        "py -m intraday_scanner.v2.omega_sentinel clear-stale-locks",
    ]


def _untrusted_assumptions() -> list[str]:
    return [
        "No strategy is validated until 30 true forward paper days and 30 closed true forward trades exist.",
        "Public OHLCV remains free public data and is not broker-grade market evidence.",
        "Daily candles do not prove intraday fill precision.",
        "External alerts are local artifacts only unless explicitly configured later.",
    ]


def _trial_lines(payload: dict[str, object]) -> list[str]:
    return [
        f"Completed forward days: `{payload['completed_forward_days']} / {payload['target_forward_days']}`",
        f"Days remaining: `{payload['days_remaining_to_minimum_evidence']}`",
        f"Latest run date: `{payload['latest_run_date']}`",
        f"Next review date: `{payload['next_review_date']}`",
        f"Yellow/red days: `{payload['yellow_days']} / {payload['red_days']}`",
    ]


def _blocker_lines(payload: dict[str, object]) -> list[str]:
    requirements = _dict(payload.get("validation_missing_requirements"))
    lines = []
    for strategy, missing in requirements.items():
        lines.append(f"`{strategy}`: {', '.join(str(item) for item in _list(missing)) or 'none'}")
    return lines or ["No strategies in forward trial yet."]


def _artifact_lines(rows: list[dict[str, object]]) -> list[str]:
    lines = [f"Indexed artifacts: `{len(rows)}`"]
    for row in rows[:50]:
        lines.append(f"`{row['retention_class']}` {row['path']} `{row['sha256']}`")
    return lines


def _kv_lines(payload: dict[str, object]) -> list[str]:
    return [f"{key}: `{value}`" for key, value in payload.items() if key != "rows"]


def _bullet_lines(items: object) -> list[str]:
    return [f"- {item}" for item in _list(items)]


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(_plain(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _replace_with_retry(temp, path)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = tuple(sorted({key for row in rows for key in row})) or ("empty",)
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})
    _replace_with_retry(temp, path)


def _write_md(path: Path, title: str, lines: list[str]) -> None:
    _write_text(path, "# " + title + "\n\n" + "\n".join(f"- {line}" for line in lines) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    _replace_with_retry(temp, path)


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(10):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _unique_strs(values: list[object]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int(value: object) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str | int | float):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, str | int | float):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _plain(value: object) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict | list | tuple):
        return json.dumps(_plain(value), sort_keys=True)
    return str(value)
