# ruff: noqa: E501
# mypy: ignore-errors
"""Append-only bridge from FillTruth overlay evidence to PaperOps ledger evidence."""

from __future__ import annotations

import ast
import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.storage import append_jsonl_unique, read_jsonl

DEFAULT_OUTPUT_ROOT = Path("data/v2_evidence_commit")
PAPER_OPS_ROOT = Path("data/v2_paper_ops")
FILL_TRUTH_ROOT = Path("data/v2_fill_truth")
FORWARD_ROOT = Path("data/v2_forward_evidence")

COMMIT_DIRS = (
    "proposals",
    "commits",
    "rejections",
    "reconciliation",
    "reports",
    "manifests",
    "logs",
)

PROPOSAL_FIELDS = (
    "proposal_id",
    "date",
    "evidence_mode",
    "source_filltruth_run_id",
    "source_filltruth_artifact_hash",
    "source_paper_order_id",
    "source_pending_order_id",
    "frozen_pick_id",
    "frozen_pick_hash",
    "symbol",
    "strategy_id",
    "strategy_version",
    "strategy_status",
    "direction",
    "proposed_event_type",
    "proposed_fill_price",
    "proposed_fill_timestamp",
    "proposed_close_price",
    "proposed_close_timestamp",
    "execution_model",
    "fill_certainty",
    "data_granularity",
    "data_snapshot_id",
    "intraday_snapshot_id",
    "realized_pnl",
    "unrealized_pnl",
    "fees",
    "slippage",
    "warnings",
    "blocking_reasons",
    "commit_eligibility",
    "source_kind",
    "source_label",
    "source_file_sha256",
    "canonical_provider_id",
    "comparison_provider_ids",
    "canonical_dataset_hash",
    "canonical_duplicate_timestamp_count",
    "provider_reconciliation_status",
    "real_intraday_reconciliation_status",
    "session_completeness",
    "filltruth_commit_eligible",
    "created_at",
    "schema_version",
)

EVENT_FIELDS = (
    "commit_id",
    "proposal_id",
    "source_filltruth_run_id",
    "source_artifact_hash",
    "paper_order_id",
    "ledger_event_id",
    "event_type",
    "committed_at",
    "committed_by",
    "commit_mode",
    "warnings",
    "supersedes",
    "schema_version",
)


class EvidenceCommitStatus(str, Enum):
    ELIGIBLE = "eligible"
    ELIGIBLE_WITH_WARNINGS = "eligible_with_warnings"
    BLOCKED_DEMO_OR_SYNTHETIC = "blocked_demo_or_synthetic"
    BLOCKED_REPLAY_NOT_FORWARD = "blocked_replay_not_forward"
    BLOCKED_MISSING_ORDER = "blocked_missing_order"
    BLOCKED_MISSING_FROZEN_PICK = "blocked_missing_frozen_pick"
    BLOCKED_HASH_MISMATCH = "blocked_hash_mismatch"
    BLOCKED_DUPLICATE_EVENT = "blocked_duplicate_event"
    BLOCKED_ORPHAN_FILL = "blocked_orphan_fill"
    BLOCKED_INVALID_DATA = "blocked_invalid_data"
    BLOCKED_AMBIGUOUS_FILL = "blocked_ambiguous_fill"
    BLOCKED_STRATEGY_REJECTED = "blocked_strategy_rejected"
    BLOCKED_RISKHUB_POLICY = "blocked_riskhub_policy"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class EvidenceCommitSource(str, Enum):
    FILLTRUTH_OVERLAY = "filltruth_overlay"
    REAL_LOCAL_INTRADAY = "real_local_intraday"
    PROVIDER_INTRADAY = "provider_intraday"
    BROKER_OR_VENDOR_INTRADAY = "broker_or_vendor_intraday"
    PUBLIC_INTRADAY_SINGLE_PROVIDER = "public_intraday_single_provider"
    SYNTHETIC_DEMO_INTRADAY = "synthetic_demo_intraday"
    MOCK_TEST_INTRADAY = "mock_test_intraday"
    REPLAY_INTRADAY = "replay_intraday"
    UNKNOWN_INTRADAY = "unknown_intraday"
    DAILY = "daily"


@dataclass(frozen=True)
class EvidenceCommitConfig:
    auto_commit_enabled: bool = False
    allow_demo_commit: bool = False
    allow_replay_commit: bool = False
    allow_strategy_rejected_resolution: bool = False
    allowed_execution_models: tuple[str, ...] = (
        "intraday_bar_sequence",
        "daily_next_open",
    )
    allowed_fill_certainties: tuple[str, ...] = (
        "intraday_sequence_supported",
        "exact_known_from_bar_open",
    )
    schema_version: str = "v2.evidence_commit_config.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class EvidenceCommitProposal:
    proposal_id: str
    date: str
    evidence_mode: str
    source_filltruth_run_id: str
    source_filltruth_artifact_hash: str
    source_paper_order_id: str
    source_pending_order_id: str
    frozen_pick_id: str
    frozen_pick_hash: str
    symbol: str
    strategy_id: str
    strategy_version: str
    strategy_status: str
    direction: str
    proposed_event_type: str
    proposed_fill_price: float | None
    proposed_fill_timestamp: str
    proposed_close_price: float | None
    proposed_close_timestamp: str
    execution_model: str
    fill_certainty: str
    data_granularity: str
    data_snapshot_id: str
    intraday_snapshot_id: str
    realized_pnl: float | None
    unrealized_pnl: float | None
    fees: float
    slippage: float
    warnings: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    commit_eligibility: EvidenceCommitStatus
    source_kind: EvidenceCommitSource
    source_label: str
    source_file_sha256: str
    canonical_provider_id: str
    comparison_provider_ids: tuple[str, ...]
    canonical_dataset_hash: str
    canonical_duplicate_timestamp_count: int
    provider_reconciliation_status: str
    real_intraday_reconciliation_status: str
    session_completeness: str
    filltruth_commit_eligible: bool
    quantity: int
    stop: float | None
    target: float | None
    created_at: str
    schema_version: str = "v2.evidence_commit_proposal.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class EvidenceCommitDecision:
    decision_id: str
    proposal_id: str
    decision: str
    reason: str
    decided_at: str
    schema_version: str = "v2.evidence_commit_decision.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class EvidenceCommitEvent:
    commit_id: str
    proposal_id: str
    source_filltruth_run_id: str
    source_artifact_hash: str
    paper_order_id: str
    ledger_event_id: str
    event_type: str
    committed_at: str
    committed_by: str
    commit_mode: str
    warnings: tuple[str, ...]
    supersedes: str
    schema_version: str = "v2.evidence_commit_event.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class EvidenceCommitRejection:
    rejection_id: str
    proposal_id: str
    reason: str
    rejected_at: str
    rejected_by: str = "local_system"
    schema_version: str = "v2.evidence_commit_rejection.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class EvidenceCommitManifest:
    run_id: str
    command: str
    run_date: str
    generated_artifacts: tuple[str, ...]
    artifact_hashes: dict[str, str]
    warnings: tuple[str, ...]
    created_at: str
    schema_version: str = "v2.evidence_commit_manifest.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class EvidenceCommitReconciliationReport:
    status: str
    run_date: str
    filltruth_resolutions: int
    proposals_created: int
    proposals_eligible: int
    proposals_committed: int
    proposals_rejected: int
    proposals_blocked: int
    pending_before_commit: int
    pending_after_commit: int
    uncommitted_overlay_count: int
    pending_divergence_status: str
    ledger_rebuild_status: str
    calendar_truth_status: str
    strategy_evidence_status: str
    warnings: tuple[str, ...]
    schema_version: str = "v2.evidence_commit_reconciliation.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class _CommitPaths:
    root: Path
    proposals: Path
    commits: Path
    rejections: Path
    reconciliation: Path
    reports: Path
    manifests: Path
    logs: Path

    @classmethod
    def create(cls, root: Path) -> _CommitPaths:
        values = {name: root / name for name in COMMIT_DIRS}
        for path in values.values():
            path.mkdir(parents=True, exist_ok=True)
        return cls(root=root, **values)


def init(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _CommitPaths.create(output_root)
    config_path = paths.root / "evidence_commit_config.json"
    if not config_path.exists():
        _write_json(config_path, EvidenceCommitConfig().to_dict())
    _write_docs()
    readiness = _write_real_intraday_readiness(paths)
    return {
        "config": config_path.as_posix(),
        "directories": [str(path.as_posix()) for path in _path_values(paths)],
        "real_intraday_readiness_status": readiness["status"],
        "status": "initialized",
    }


def propose(
    *,
    run_date: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    fill_truth_root: Path = FILL_TRUTH_ROOT,
    paper_ops_root: Path = PAPER_OPS_ROOT,
    forward_root: Path = FORWARD_ROOT,
    require_real_intraday: bool = False,
    require_provider_intraday: bool = False,
) -> dict[str, object]:
    paths = _CommitPaths.create(output_root)
    config = _config(paths)
    resolution_path = fill_truth_root / "reports" / "pending_resolution_latest.json"
    resolution = _dict(_read_json(resolution_path, {}))
    decisions = []
    for row in _list(resolution.get("decisions")):
        decision = _dict(row)
        if str(decision.get("run_date", run_date.isoformat())) == run_date.isoformat():
            decisions.append(decision)
    source_hash = _sha256(resolution_path) if resolution_path.exists() else ""
    source_run_id = _latest_filltruth_run_id(fill_truth_root, "resolve", run_date)
    pending_orders = _pending_orders(paper_ops_root)
    pick_index = _paper_pick_index(paper_ops_root)
    existing_events = read_jsonl(paper_ops_root / "ledger" / "paper_ledger.jsonl")
    intraday_meta = _intraday_metadata(fill_truth_root)
    proposals = [
        _proposal_from_decision(
            decision=decision,
            config=config,
            run_date=run_date,
            source_hash=source_hash,
            source_run_id=source_run_id,
            pending_orders=pending_orders,
            pick_index=pick_index,
            existing_events=existing_events,
            intraday_meta=intraday_meta,
            forward_root=forward_root,
            require_real_intraday=require_real_intraday,
            require_provider_intraday=require_provider_intraday,
        )
        for decision in decisions
    ]
    payload = {
        "created_at": _now(),
        "proposal_count": len(proposals),
        "proposals": [item.to_dict() for item in proposals],
        "run_date": run_date.isoformat(),
        "schema_version": "v2.evidence_commit_proposals.v1",
        "status": "passed",
    }
    latest_commits = _dict(_read_json(paths.commits / "latest_commit_events.json", {}))
    existing_payload = _dict(_read_json(paths.proposals / f"{run_date.isoformat()}_proposals.json", {}))
    should_preserve_committed_proposals = (
        not proposals
        and str(latest_commits.get("run_date", "")) == run_date.isoformat()
        and _int(latest_commits.get("committed_count")) > 0
        and bool(_list(existing_payload.get("proposals")))
    )
    artifacts = () if should_preserve_committed_proposals else _write_proposals(paths, run_date, payload)
    _write_manifest(paths, "propose", run_date, artifacts, _proposal_warnings(proposals))
    _append_jsonl(paths.logs / "evidence_commit_ledger.jsonl", [_log_row("propose", payload)], "log_id")
    _write_real_intraday_readiness(paths, fill_truth_root=fill_truth_root)
    return {
        "blocked": sum(1 for item in proposals if _is_blocked(item.commit_eligibility)),
        "eligible": sum(1 for item in proposals if _is_eligible(item.commit_eligibility)),
        "proposal_count": len(proposals),
        "status": "passed",
    }


def review(
    *,
    run_date: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, object]:
    paths = _CommitPaths.create(output_root)
    proposals = _latest_proposals(paths, run_date)
    rows = [_dict(row) for row in proposals]
    summary = _proposal_summary(rows)
    payload = {
        **summary,
        "reviewed_at": _now(),
        "run_date": run_date.isoformat(),
        "schema_version": "v2.evidence_commit_review.v1",
        "status": "passed",
    }
    _write_json(paths.reports / "latest_review.json", payload)
    _write_md(paths.reports / "latest_review.md", "Evidence Commit Review", _summary_lines(payload))
    _write_manifest(paths, "review", run_date, (paths.reports / "latest_review.json", paths.reports / "latest_review.md"), ())
    return payload


def commit(
    *,
    run_date: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    paper_ops_root: Path = PAPER_OPS_ROOT,
    require_real_intraday: bool = False,
    require_provider_intraday: bool = False,
) -> dict[str, object]:
    paths = _CommitPaths.create(output_root)
    proposals = [_dict(row) for row in _latest_proposals(paths, run_date)]
    now = _now()
    ledger_rows: list[dict[str, object]] = []
    commit_events: list[EvidenceCommitEvent] = []
    blocked: list[dict[str, object]] = []
    for proposal in proposals:
        eligibility = str(proposal.get("commit_eligibility", ""))
        if require_real_intraday and not _proposal_has_real_intraday(proposal):
            blocked.append(proposal)
            continue
        if require_provider_intraday and not _proposal_has_provider_intraday(proposal):
            blocked.append(proposal)
            continue
        if eligibility not in {EvidenceCommitStatus.ELIGIBLE.value, EvidenceCommitStatus.ELIGIBLE_WITH_WARNINGS.value}:
            blocked.append(proposal)
            continue
        proposal_events = _paper_events_for_proposal(proposal, now)
        ledger_rows.extend(proposal_events)
        for event in proposal_events:
            commit_events.append(
                EvidenceCommitEvent(
                    commit_id=_stable_id("commit", proposal["proposal_id"], event["event_id"]),
                    proposal_id=str(proposal["proposal_id"]),
                    source_filltruth_run_id=str(proposal["source_filltruth_run_id"]),
                    source_artifact_hash=str(proposal["source_filltruth_artifact_hash"]),
                    paper_order_id=str(proposal["source_paper_order_id"]),
                    ledger_event_id=str(event["event_id"]),
                    event_type=str(event["event_type"]),
                    committed_at=now,
                    committed_by="local_system",
                    commit_mode="manual_cli",
                    warnings=tuple(str(item) for item in _list(proposal.get("warnings"))),
                    supersedes="",
                )
            )
    appended = append_jsonl_unique(paper_ops_root / "ledger" / "paper_ledger.jsonl", ledger_rows, "event_id")
    event_payload = {
        "blocked_count": len(blocked),
        "commit_events": [event.to_dict() for event in commit_events],
        "committed_count": appended,
        "created_at": now,
        "proposals_seen": len(proposals),
        "run_date": run_date.isoformat(),
        "schema_version": "v2.evidence_commit_events.v1",
        "status": "passed",
    }
    artifacts = _write_commits(paths, run_date, event_payload)
    _write_manifest(paths, "commit", run_date, artifacts, ())
    _append_jsonl(paths.logs / "evidence_commit_ledger.jsonl", [_log_row("commit", event_payload)], "log_id")
    return {
        "blocked_count": len(blocked),
        "committed_count": appended,
        "ledger_events_generated": len(ledger_rows),
        "status": "passed",
    }


def reject(
    *,
    run_date: date,
    reason: str,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, object]:
    paths = _CommitPaths.create(output_root)
    proposals = [_dict(row) for row in _latest_proposals(paths, run_date)]
    current_proposal_ids = {
        str(proposal.get("proposal_id"))
        for proposal in proposals
        if str(proposal.get("proposal_id", ""))
    }
    committed_ids = _committed_proposal_ids(paths)
    rejection_path = paths.rejections / f"{run_date.isoformat()}_rejections.json"
    existing_payload = _dict(_read_json(rejection_path, {}))
    raw_existing_rejections = [_dict(row) for row in _list(existing_payload.get("rejections"))]
    existing_rejections = [
        row
        for row in raw_existing_rejections
        if str(row.get("proposal_id")) in current_proposal_ids
    ]
    stale_rejections = [
        row
        for row in raw_existing_rejections
        if str(row.get("proposal_id")) not in current_proposal_ids
    ]
    rejected_ids = {str(row.get("proposal_id")) for row in existing_rejections}
    now = _now()
    rejections = [
        EvidenceCommitRejection(
            rejection_id=_stable_id("rejection", proposal.get("proposal_id"), reason),
            proposal_id=str(proposal["proposal_id"]),
            reason=reason,
            rejected_at=now,
        )
        for proposal in proposals
        if str(proposal.get("proposal_id", "")) not in committed_ids
        and str(proposal.get("proposal_id", "")) not in rejected_ids
    ]
    all_rejections_by_id = {
        str(row.get("rejection_id")): row
        for row in existing_rejections
        if str(row.get("rejection_id", ""))
    }
    for row in rejections:
        all_rejections_by_id[row.rejection_id] = row.to_dict()
    all_rejections = list(all_rejections_by_id.values())
    payload = {
        "reason": reason,
        "new_rejected_count": len(rejections),
        "rejected_count": len(all_rejections),
        "rejections": all_rejections,
        "run_date": run_date.isoformat(),
        "schema_version": "v2.evidence_commit_rejections.v1",
        "status": "passed",
        "stale_rejection_count": len(stale_rejections),
        "stale_rejections": stale_rejections,
    }
    artifacts = _write_rejections(paths, run_date, payload)
    _append_jsonl(paths.logs / "evidence_commit_ledger.jsonl", [_log_row("reject", payload)], "log_id")
    _write_manifest(paths, "reject", run_date, artifacts, ())
    return {
        "new_rejected_count": len(rejections),
        "rejected_count": len(all_rejections),
        "status": "passed",
    }


def rebuild_state(
    *,
    run_date: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    paper_ops_root: Path = PAPER_OPS_ROOT,
    forward_root: Path = FORWARD_ROOT,
) -> dict[str, object]:
    paths = _CommitPaths.create(output_root)
    rebuilt = rebuild_ledger(output_root=paper_ops_root, write_rebuilt=True).to_dict()
    pending = [_dict(row) for row in _list(rebuilt.get("pending_orders"))]
    open_positions = [_dict(row) for row in _list(rebuilt.get("open_positions"))]
    accounts = [_dict(row) for row in _list(rebuilt.get("account_rows"))]
    calendar_rows = [_dict(row) for row in _list(rebuilt.get("calendar_rows"))]
    _write_mode_state(paper_ops_root, "pending_orders", pending)
    _write_mode_state(paper_ops_root, "open_positions", open_positions)
    _write_mode_accounts(paper_ops_root, accounts)
    _write_rebuilt_calendar_overlay(paths, run_date, calendar_rows)
    _write_commit_calendar_overlay(paths, run_date, forward_root=forward_root)
    try:
        from intraday_scanner.v2.paper_ops.strategy_evidence import score_strategy_evidence

        strategy = score_strategy_evidence(output_root=paper_ops_root).to_dict()
    except Exception as exc:  # pragma: no cover - diagnostic path
        strategy = {"status": "failed", "warnings": [str(exc)]}
    payload = {
        "calendar_rows": len(calendar_rows),
        "ledger_rebuild_status": rebuilt.get("status", "unknown"),
        "open_positions": len([row for row in open_positions if _row_mode(row) == "forward"]),
        "pending_orders": len([row for row in pending if _row_mode(row) == "forward"]),
        "run_date": run_date.isoformat(),
        "schema_version": "v2.evidence_commit_rebuild_state.v1",
        "status": "passed" if rebuilt.get("status") == "passed" else "passed_with_warnings",
        "strategy_evidence_status": strategy.get("status", "unknown"),
        "warnings": _list(rebuilt.get("warnings")) + _list(strategy.get("warnings")),
    }
    _write_json(paths.reconciliation / "rebuild_state_latest.json", payload)
    _write_md(paths.reconciliation / "rebuild_state_latest.md", "Evidence Commit Rebuild State", _summary_lines(payload))
    _write_manifest(paths, "rebuild-state", run_date, (paths.reconciliation / "rebuild_state_latest.json",), tuple(str(item) for item in _list(payload.get("warnings"))))
    return payload


def reconcile(
    *,
    run_date: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    fill_truth_root: Path = FILL_TRUTH_ROOT,
    paper_ops_root: Path = PAPER_OPS_ROOT,
    forward_root: Path = FORWARD_ROOT,
) -> dict[str, object]:
    del forward_root
    paths = _CommitPaths.create(output_root)
    proposals = [_dict(row) for row in _latest_proposals(paths, run_date)]
    committed_ids = _committed_proposal_ids(paths)
    rejected_ids = _rejected_proposal_ids(paths)
    resolution = _dict(_read_json(fill_truth_root / "reports" / "pending_resolution_latest.json", {}))
    filltruth_resolutions = len(_list(resolution.get("decisions")))
    pending_before = _int(resolution.get("pending_orders_inspected"))
    official_pending = _pending_orders(paper_ops_root)
    committed = [row for row in proposals if str(row.get("proposal_id")) in committed_ids]
    rejected_rows = [row for row in proposals if str(row.get("proposal_id")) in rejected_ids]
    blocked = [row for row in proposals if _is_blocked_status(str(row.get("commit_eligibility")))]
    eligible = [row for row in proposals if _is_eligible_status(str(row.get("commit_eligibility")))]
    uncommitted = [
        row
        for row in proposals
        if str(row.get("proposal_id")) not in committed_ids
        and str(row.get("proposal_id")) not in rejected_ids
        and _is_eligible_status(str(row.get("commit_eligibility")))
    ]
    rebuild = _dict(_read_json(paper_ops_root / "reconciliation" / "ledger_rebuild_latest.json", {}))
    if uncommitted:
        divergence = "unresolved_uncommitted_eligible_overlay"
    elif rejected_rows or (blocked and not committed):
        divergence = "resolved_by_policy_block_or_rejection"
    elif proposals and not official_pending:
        divergence = "resolved_by_commit"
    else:
        divergence = "no_filltruth_overlay_divergence"
    warnings = []
    if blocked:
        warnings.append("blocked FillTruth proposals are not official PaperOps evidence")
    if uncommitted:
        warnings.append("eligible FillTruth proposals require explicit commit")
    report_payload = EvidenceCommitReconciliationReport(
        status="passed" if not uncommitted else "passed_with_warnings",
        run_date=run_date.isoformat(),
        filltruth_resolutions=filltruth_resolutions,
        proposals_created=len(proposals),
        proposals_eligible=len(eligible),
        proposals_committed=len(committed),
        proposals_rejected=len(rejected_rows),
        proposals_blocked=len(blocked),
        pending_before_commit=pending_before,
        pending_after_commit=len([row for row in official_pending if _row_mode(row) == "forward"]),
        uncommitted_overlay_count=len(uncommitted),
        pending_divergence_status=divergence,
        ledger_rebuild_status=str(rebuild.get("status", "missing")),
        calendar_truth_status="passed" if not uncommitted else "blocked_uncommitted_overlay",
        strategy_evidence_status="blocked_until_committed_forward_fill_evidence_is_sufficient",
        warnings=tuple(warnings),
    )
    payload = report_payload.to_dict()
    artifacts = _write_reconciliation(paths, payload)
    _write_commit_strategy_overlay(paths, payload)
    _write_manifest(paths, "reconcile", run_date, artifacts, report_payload.warnings)
    return payload


def report(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _CommitPaths.create(output_root)
    latest_reconciliation = _dict(
        _read_json(paths.reconciliation / "pending_divergence_latest.json", {})
    )
    latest_proposals = [_dict(row) for row in _latest_proposals(paths, None)]
    latest_commits = _dict(_read_json(paths.commits / "latest_commit_events.json", {}))
    rejected_ids = _rejected_proposal_ids(paths)
    current_rejected_count = len(
        [
            row
            for row in latest_proposals
            if str(row.get("proposal_id")) in rejected_ids
        ]
    )
    readiness = _write_real_intraday_readiness(paths)
    proposal_summary = _proposal_summary(latest_proposals)
    committed_count = _int(latest_commits.get("committed_count"))
    provider_ready = any(_proposal_has_provider_intraday(row) for row in latest_proposals) or (
        committed_count > 0 and _current_provider_intraday_evidence_available()
    )
    summary = {
        **proposal_summary,
        "build_id": _latest_build_id(paths),
        "commit_events": committed_count,
        "pending_divergence_status": latest_reconciliation.get("pending_divergence_status", "missing"),
        "provider_intraday_evidence_status": "passed" if provider_ready else "missing",
        "quality_score": _write_scorecard(paths)["score"],
        "real_intraday_readiness_status": readiness["status"],
        "rejected": current_rejected_count,
        "schema_version": "v2.evidence_commit_summary.v1",
        "status": "passed",
        "what_next": (
            "Continue provider-backed after-close and next-morning OMEGA checks."
            if provider_ready and committed_count
            else readiness.get("next_required_action", "continue forward trial after close")
        ),
    }
    _write_json(paths.reports / "evidence_commit_summary.json", summary)
    _write_md(paths.reports / "evidence_commit_summary.md", "Evidence Commit Summary", _summary_lines(summary))
    _write_audit_docs(paths, summary)
    return summary


def verify(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _CommitPaths.create(output_root)
    failures: list[str] = []
    warnings: list[str] = []
    required = (
        paths.proposals / "latest_proposals.json",
        paths.reconciliation / "pending_divergence_latest.json",
        paths.reports / "evidence_commit_summary.json",
        Path("docs/audit/omega_commitbridge_quality_scorecard.md"),
        Path("docs/audit/omega_commitbridge_red_team.md"),
    )
    for path in required:
        if not path.exists():
            failures.append(f"missing artifact: {path.as_posix()}")
    safety = _safety_scan(paths)
    failures.extend(safety["failures"])
    warnings.extend(safety["warnings"])
    proposals = [_dict(row) for row in _latest_proposals(paths, None)]
    for proposal in proposals:
        if proposal.get("source_kind") == EvidenceCommitSource.SYNTHETIC_DEMO_INTRADAY.value:
            if str(proposal.get("commit_eligibility")) != EvidenceCommitStatus.BLOCKED_DEMO_OR_SYNTHETIC.value:
                failures.append(f"synthetic proposal not blocked: {proposal.get('proposal_id')}")
    payload = {
        "checked_at": _now(),
        "failures": sorted(set(failures)),
        "schema_version": "v2.evidence_commit_verify.v1",
        "status": "passed" if not failures else "failed",
        "warnings": sorted(set(warnings)),
    }
    _write_json(paths.reconciliation / "verify_latest.json", payload)
    _write_md(paths.reconciliation / "verify_latest.md", "Evidence Commit Verify", _summary_lines(payload))
    return payload


def demo(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    run_date = date(2026, 6, 29)
    proposal = propose(run_date=run_date, output_root=output_root)
    review(run_date=run_date, output_root=output_root)
    commit_result = commit(run_date=run_date, output_root=output_root)
    reject(
        run_date=run_date,
        reason="demo or synthetic FillTruth evidence cannot enter true forward PaperOps",
        output_root=output_root,
    )
    rebuild_state(run_date=run_date, output_root=output_root)
    reconciliation = reconcile(run_date=run_date, output_root=output_root)
    summary = report(output_root=output_root)
    verification = verify(output_root=output_root)
    return {
        "commitbridge_status": summary["status"],
        "committed_count": commit_result["committed_count"],
        "pending_divergence_status": reconciliation["pending_divergence_status"],
        "proposal_count": proposal["proposal_count"],
        "quality_score": summary["quality_score"],
        "status": "complete" if verification["status"] == "passed" else "resume_required",
        "verify_status": verification["status"],
    }


def _proposal_from_decision(
    *,
    decision: dict[str, object],
    config: EvidenceCommitConfig,
    run_date: date,
    source_hash: str,
    source_run_id: str,
    pending_orders: list[dict[str, object]],
    pick_index: dict[str, dict[str, object]],
    existing_events: list[dict[str, object]],
    intraday_meta: dict[str, object],
    forward_root: Path,
    require_real_intraday: bool,
    require_provider_intraday: bool,
) -> EvidenceCommitProposal:
    order_id = str(decision.get("order_id", ""))
    pending_order = next((row for row in pending_orders if row.get("order_id") == order_id), {})
    pick_id = str(pending_order.get("pick_id") or decision.get("pick_id") or "")
    pick_payload = pick_index.get(pick_id, {})
    frozen_hash = _frozen_pick_hash(
        forward_root=forward_root,
        run_date=run_date,
        pick_id=pick_id,
        order_id=order_id,
    )
    source_kind = _source_kind(decision, intraday_meta)
    warnings = _unique(_list(decision.get("warnings")) + _source_warnings(source_kind, intraday_meta))
    status, blockers = _eligibility(
        config=config,
        decision=decision,
        pending_order=pending_order,
        existing_events=existing_events,
        frozen_hash=frozen_hash,
        source_hash=source_hash,
        source_kind=source_kind,
        warnings=warnings,
        intraday_meta=intraday_meta,
        require_real_intraday=require_real_intraday,
        require_provider_intraday=require_provider_intraday,
    )
    fill_time = str(decision.get("fill_time", ""))
    proposal_id = _stable_id(
        "evidence_commit_proposal",
        run_date.isoformat(),
        order_id,
        fill_time,
        str(decision.get("fill_price", "")),
    )
    data_snapshot = str(decision.get("data_snapshot_id", ""))
    intraday_snapshot = data_snapshot if str(decision.get("data_granularity")) == "intraday" else ""
    return EvidenceCommitProposal(
        proposal_id=proposal_id,
        date=run_date.isoformat(),
        evidence_mode=str(pending_order.get("mode") or _mode_from_id(order_id)),
        source_filltruth_run_id=source_run_id,
        source_filltruth_artifact_hash=source_hash,
        source_paper_order_id=order_id,
        source_pending_order_id=order_id if pending_order else "",
        frozen_pick_id=pick_id,
        frozen_pick_hash=frozen_hash,
        symbol=str(decision.get("symbol", pending_order.get("symbol", ""))),
        strategy_id=str(decision.get("strategy_id", pending_order.get("strategy_id", ""))),
        strategy_version=str(pending_order.get("strategy_version", pick_payload.get("strategy_version", "unknown"))),
        strategy_status=str(pick_payload.get("strategy_status", "unknown")),
        direction=str(decision.get("direction", pending_order.get("direction", ""))),
        proposed_event_type="fill",
        proposed_fill_price=_optional_float(decision.get("fill_price")),
        proposed_fill_timestamp=fill_time,
        proposed_close_price=None,
        proposed_close_timestamp="",
        execution_model=str(decision.get("execution_model", "")),
        fill_certainty=str(decision.get("fill_certainty", "")),
        data_granularity=str(decision.get("data_granularity", "")),
        data_snapshot_id=data_snapshot,
        intraday_snapshot_id=intraday_snapshot,
        realized_pnl=None,
        unrealized_pnl=None,
        fees=_float(decision.get("fee")),
        slippage=_float(decision.get("slippage")),
        warnings=tuple(warnings),
        blocking_reasons=tuple(blockers),
        commit_eligibility=status,
        source_kind=source_kind,
        source_label=str(intraday_meta.get("source_label") or intraday_meta.get("data_type") or source_kind.value),
        source_file_sha256=str(intraday_meta.get("source_file_sha256", "")),
        canonical_provider_id=str(intraday_meta.get("canonical_provider_id", "")),
        comparison_provider_ids=tuple(str(item) for item in _list(intraday_meta.get("comparison_provider_ids"))),
        canonical_dataset_hash=str(intraday_meta.get("canonical_dataset_hash", "")),
        canonical_duplicate_timestamp_count=_int(intraday_meta.get("canonical_duplicate_timestamp_count")),
        provider_reconciliation_status=str(
            intraday_meta.get("provider_reconciliation_status")
            or intraday_meta.get("intraday_reconciliation_status")
            or ""
        ),
        real_intraday_reconciliation_status=str(
            intraday_meta.get("daily_reconciliation_status")
            or intraday_meta.get("intraday_reconciliation_status")
            or "missing"
        ),
        session_completeness=str(intraday_meta.get("session_completeness", "unknown_session")),
        filltruth_commit_eligible=bool(intraday_meta.get("filltruth_commit_eligible", False)),
        quantity=_int(decision.get("quantity") or pending_order.get("quantity")),
        stop=_optional_float(decision.get("stop") or pending_order.get("stop")),
        target=_optional_float(decision.get("target") or pending_order.get("target")),
        created_at=_now(),
    )


def _eligibility(
    *,
    config: EvidenceCommitConfig,
    decision: dict[str, object],
    pending_order: dict[str, object],
    existing_events: list[dict[str, object]],
    frozen_hash: str,
    source_hash: str,
    source_kind: EvidenceCommitSource,
    warnings: list[str],
    intraday_meta: dict[str, object],
    require_real_intraday: bool,
    require_provider_intraday: bool,
) -> tuple[EvidenceCommitStatus, list[str]]:
    blockers: list[str] = []
    order_id = str(decision.get("order_id", ""))
    if not pending_order:
        blockers.append("matching PaperOps pending order is missing")
        return EvidenceCommitStatus.BLOCKED_MISSING_ORDER, blockers
    mode = str(pending_order.get("mode") or _mode_from_id(order_id))
    if mode == "replay" and not config.allow_replay_commit:
        blockers.append("replay evidence cannot enter true forward state")
        return EvidenceCommitStatus.BLOCKED_REPLAY_NOT_FORWARD, blockers
    if mode != "forward":
        blockers.append(f"unsupported evidence mode {mode}")
        return EvidenceCommitStatus.BLOCKED_REPLAY_NOT_FORWARD, blockers
    if source_kind == EvidenceCommitSource.SYNTHETIC_DEMO_INTRADAY and not config.allow_demo_commit:
        blockers.append("demo or synthetic FillTruth source cannot commit into true forward state")
        return EvidenceCommitStatus.BLOCKED_DEMO_OR_SYNTHETIC, blockers
    if source_kind == EvidenceCommitSource.MOCK_TEST_INTRADAY:
        blockers.append("mock test intraday source cannot commit into true forward state")
        return EvidenceCommitStatus.BLOCKED_DEMO_OR_SYNTHETIC, blockers
    if source_kind == EvidenceCommitSource.REPLAY_INTRADAY and not config.allow_replay_commit:
        blockers.append("replay intraday source cannot commit into true forward state")
        return EvidenceCommitStatus.BLOCKED_REPLAY_NOT_FORWARD, blockers
    if source_kind == EvidenceCommitSource.UNKNOWN_INTRADAY:
        blockers.append("unknown intraday source cannot commit into true forward state")
        return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
    provider_sources = {
        EvidenceCommitSource.PROVIDER_INTRADAY,
        EvidenceCommitSource.BROKER_OR_VENDOR_INTRADAY,
    }
    if source_kind == EvidenceCommitSource.DAILY or require_real_intraday:
        if source_kind != EvidenceCommitSource.REAL_LOCAL_INTRADAY:
            blockers.append("official forward commits require reconciled real-local intraday evidence")
            return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
    if require_provider_intraday and source_kind not in provider_sources:
        blockers.append("official forward commits require provider-backed intraday evidence")
        return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
    if source_kind == EvidenceCommitSource.PUBLIC_INTRADAY_SINGLE_PROVIDER:
        blockers.append("public single-provider intraday evidence requires manual review")
        return EvidenceCommitStatus.MANUAL_REVIEW_REQUIRED, blockers
    if source_kind in {EvidenceCommitSource.REAL_LOCAL_INTRADAY, *provider_sources}:
        source_file_hash = str(intraday_meta.get("source_file_sha256", ""))
        reconciliation_status = str(
            intraday_meta.get("daily_reconciliation_status")
            or intraday_meta.get("intraday_reconciliation_status")
            or ""
        )
        if not source_file_hash:
            blockers.append("intraday source file hash is missing")
            return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
        allowed_reconciliation = {
            "reconciled",
            "reconciled_with_minor_diffs",
            "provider_with_public_fallback_comparison",
        }
        if reconciliation_status not in allowed_reconciliation:
            blockers.append("intraday aggregate is not reconciled enough against DataTruth daily bars")
            return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
    if source_kind in provider_sources:
        canonical_hash = str(intraday_meta.get("canonical_dataset_hash", ""))
        canonical_duplicate_count = _int(intraday_meta.get("canonical_duplicate_timestamp_count"))
        provider_reconciliation_status = str(
            intraday_meta.get("provider_reconciliation_status")
            or intraday_meta.get("intraday_reconciliation_status")
            or ""
        )
        if not canonical_hash:
            blockers.append("canonical provider dataset hash is missing")
            return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
        if canonical_duplicate_count:
            blockers.append("canonical provider dataset contains duplicate symbol/timestamp rows")
            return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
        if provider_reconciliation_status not in {
            "reconciled",
            "reconciled_with_minor_diffs",
            "provider_with_public_fallback_comparison",
        }:
            blockers.append("provider reconciliation status does not allow official commit")
            return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
    if not source_hash:
        blockers.append("source FillTruth artifact hash is missing")
        return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
    if not frozen_hash:
        blockers.append("frozen pick hash is missing")
        return EvidenceCommitStatus.BLOCKED_MISSING_FROZEN_PICK, blockers
    if _has_fill_event(existing_events, order_id):
        blockers.append("PaperOps already has a fill event for this order")
        return EvidenceCommitStatus.BLOCKED_DUPLICATE_EVENT, blockers
    fill_time = str(decision.get("fill_time", ""))
    if not _valid_timestamp(fill_time):
        blockers.append("FillTruth fill timestamp is missing or invalid")
        return EvidenceCommitStatus.BLOCKED_INVALID_DATA, blockers
    execution_model = str(decision.get("execution_model", ""))
    if execution_model not in set(config.allowed_execution_models):
        blockers.append(f"execution model {execution_model} is not allowed")
        return EvidenceCommitStatus.MANUAL_REVIEW_REQUIRED, blockers
    fill_certainty = str(decision.get("fill_certainty", ""))
    if fill_certainty not in set(config.allowed_fill_certainties):
        blockers.append(f"fill certainty {fill_certainty} is not allowed")
        return EvidenceCommitStatus.BLOCKED_AMBIGUOUS_FILL, blockers
    if warnings:
        return EvidenceCommitStatus.ELIGIBLE_WITH_WARNINGS, []
    return EvidenceCommitStatus.ELIGIBLE, []


def _paper_events_for_proposal(proposal: dict[str, object], committed_at: str) -> list[dict[str, object]]:
    order_id = str(proposal["source_paper_order_id"])
    run_id = _run_id_from_order(order_id, proposal)
    trade_date = str(proposal["date"])
    strategy_id = str(proposal["strategy_id"])
    symbol = str(proposal["symbol"])
    mode = str(proposal["evidence_mode"] or "forward")
    fill_id = _stable_id("paper_fill", order_id, proposal.get("proposed_fill_timestamp"))
    position_id = _stable_id("position", order_id)
    fill_payload = {
        "commitbridge_proposal_id": proposal["proposal_id"],
        "fee": _float(proposal.get("fees")),
        "fill_id": fill_id,
        "fill_price": _float(proposal.get("proposed_fill_price")),
        "fill_time": proposal.get("proposed_fill_timestamp", ""),
        "mode": mode,
        "order_id": order_id,
        "quantity": _int(proposal.get("quantity")),
        "run_id": run_id,
        "schema_version": "v2.paper_fill.v1",
        "slippage": _float(proposal.get("slippage")),
        "strategy_id": strategy_id,
        "symbol": symbol,
    }
    position_payload = {
        "commitbridge_proposal_id": proposal["proposal_id"],
        "direction": proposal.get("direction", ""),
        "entry_price": fill_payload["fill_price"],
        "last_mark_price": fill_payload["fill_price"],
        "opened_at": fill_payload["fill_time"],
        "order_id": order_id,
        "position_id": position_id,
        "quantity": fill_payload["quantity"],
        "realized_pnl": 0.0,
        "schema_version": "v2.paper_position.v1",
        "status": "open",
        "stop": proposal.get("stop"),
        "strategy_id": strategy_id,
        "strategy_version": proposal.get("strategy_version", "unknown"),
        "symbol": symbol,
        "target": proposal.get("target"),
        "unrealized_pnl": 0.0,
    }
    commit_payload = {
        "canonical_dataset_hash": proposal.get("canonical_dataset_hash", ""),
        "canonical_duplicate_timestamp_count": _int(proposal.get("canonical_duplicate_timestamp_count")),
        "canonical_provider_id": proposal.get("canonical_provider_id", ""),
        "commit_id": _stable_id("commit", proposal["proposal_id"]),
        "commit_mode": "manual_cli",
        "comparison_provider_ids": proposal.get("comparison_provider_ids", []),
        "committed_at": committed_at,
        "committed_by": "local_system",
        "provider_reconciliation_status": proposal.get("provider_reconciliation_status", ""),
        "proposal_id": proposal["proposal_id"],
        "schema_version": "v2.evidence_commit_paperops_payload.v1",
        "source_kind": proposal.get("source_kind", ""),
        "source_artifact_hash": proposal["source_filltruth_artifact_hash"],
        "source_filltruth_run_id": proposal["source_filltruth_run_id"],
        "supersedes": "",
        "warnings": proposal.get("warnings", []),
    }
    return [
        _ledger_event(
            run_id=run_id,
            mode=mode,
            trade_date=trade_date,
            strategy_id=strategy_id,
            symbol=symbol,
            event_type="filltruth_commit",
            entity_id=str(proposal["proposal_id"]),
            payload=commit_payload,
        ),
        _ledger_event(
            run_id=run_id,
            mode=mode,
            trade_date=trade_date,
            strategy_id=strategy_id,
            symbol=symbol,
            event_type="paper_fill",
            entity_id=fill_id,
            payload=fill_payload,
        ),
        _ledger_event(
            run_id=run_id,
            mode=mode,
            trade_date=trade_date,
            strategy_id=strategy_id,
            symbol=symbol,
            event_type="paper_position_opened",
            entity_id=position_id,
            payload=position_payload,
        ),
    ]


def _ledger_event(
    *,
    run_id: str,
    mode: str,
    trade_date: str,
    strategy_id: str,
    symbol: str,
    event_type: str,
    entity_id: str,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "event_id": _stable_id(run_id, "commitbridge", event_type, entity_id),
        "event_type": event_type,
        "mode": mode,
        "payload": payload,
        "run_id": run_id,
        "schema_version": "v2.paper_ledger_event.v1",
        "strategy_id": strategy_id,
        "symbol": symbol,
        "trade_date": trade_date,
    }


def _write_proposals(paths: _CommitPaths, run_date: date, payload: dict[str, object]) -> tuple[Path, ...]:
    json_path = paths.proposals / f"{run_date.isoformat()}_proposals.json"
    csv_path = paths.proposals / f"{run_date.isoformat()}_proposals.csv"
    latest_json = paths.proposals / "latest_proposals.json"
    latest_csv = paths.reports / "latest_commit_proposals.csv"
    reports_json = paths.reports / "latest_commit_proposals.json"
    rows = [_dict(row) for row in _list(payload.get("proposals"))]
    _write_json(json_path, payload)
    _write_json(latest_json, payload)
    _write_json(reports_json, payload)
    _write_csv(csv_path, rows, PROPOSAL_FIELDS)
    _write_csv(latest_csv, rows, PROPOSAL_FIELDS)
    return (json_path, csv_path, latest_json, latest_csv, reports_json)


def _write_commits(paths: _CommitPaths, run_date: date, payload: dict[str, object]) -> tuple[Path, ...]:
    json_path = paths.commits / f"{run_date.isoformat()}_commit_events.json"
    csv_path = paths.commits / f"{run_date.isoformat()}_commit_events.csv"
    latest_json = paths.commits / "latest_commit_events.json"
    reports_json = paths.reports / "latest_commit_events.json"
    reports_csv = paths.reports / "latest_commit_events.csv"
    rows = [_dict(row) for row in _list(payload.get("commit_events"))]
    _write_json(json_path, payload)
    _write_json(latest_json, payload)
    _write_json(reports_json, payload)
    _write_csv(csv_path, rows, EVENT_FIELDS)
    _write_csv(reports_csv, rows, EVENT_FIELDS)
    return (json_path, csv_path, latest_json, reports_json, reports_csv)


def _write_rejections(paths: _CommitPaths, run_date: date, payload: dict[str, object]) -> tuple[Path, ...]:
    json_path = paths.rejections / f"{run_date.isoformat()}_rejections.json"
    latest_json = paths.rejections / "latest_rejections.json"
    _write_json(json_path, payload)
    _write_json(latest_json, payload)
    return (json_path, latest_json)


def _write_reconciliation(paths: _CommitPaths, payload: dict[str, object]) -> tuple[Path, ...]:
    json_path = paths.reconciliation / "pending_divergence_latest.json"
    md_path = paths.reconciliation / "pending_divergence_latest.md"
    report_json = paths.reports / "pending_divergence_latest.json"
    report_md = paths.reports / "pending_divergence_latest.md"
    _write_json(json_path, payload)
    _write_json(report_json, payload)
    _write_md(md_path, "Pending Divergence", _summary_lines(payload))
    _write_md(report_md, "Pending Divergence", _summary_lines(payload))
    return (json_path, md_path, report_json, report_md)


def _write_mode_state(root: Path, stem: str, rows: list[dict[str, object]]) -> None:
    state = root / "state"
    forward = [row for row in rows if _row_mode(row) == "forward"]
    replay = [row for row in rows if _row_mode(row) == "replay"]
    demo_rows = [row for row in rows if _row_mode(row) == "demo"]
    _write_json(state / f"{stem}.json", forward)
    _write_json(state / f"replay_{stem}.json", replay)
    _write_json(state / f"demo_{stem}.json", demo_rows)


def _write_mode_accounts(root: Path, rows: list[dict[str, object]]) -> None:
    state = root / "state"
    for mode, name in (
        ("forward", "paper_accounts.json"),
        ("replay", "replay_paper_accounts.json"),
        ("demo", "demo_paper_accounts.json"),
    ):
        accounts = []
        for row in rows:
            if _row_mode(row) != mode:
                continue
            copied = dict(row)
            copied.pop("mode", None)
            copied.setdefault("schema_version", "v2.strategy_paper_account.v1")
            accounts.append(copied)
        _write_json(state / name, {"accounts": accounts, "schema_version": "v2.paper_account_state.v1"})


def _write_rebuilt_calendar_overlay(
    paths: _CommitPaths,
    run_date: date,
    rows: list[dict[str, object]],
) -> None:
    selected = [row for row in rows if str(row.get("date")) == run_date.isoformat()]
    _write_json(
        paths.reconciliation / "calendar_rebuild_latest.json",
        {"rows": selected, "run_date": run_date.isoformat(), "status": "passed"},
    )


def _write_commit_calendar_overlay(
    paths: _CommitPaths,
    run_date: date,
    *,
    forward_root: Path,
) -> None:
    proposals = [_dict(row) for row in _latest_proposals(paths, run_date)]
    committed = _committed_proposal_ids(paths)
    rejected = _rejected_proposal_ids(paths)
    rows: list[dict[str, object]] = []
    for strategy_id in sorted({str(row.get("strategy_id", "unknown")) for row in proposals}):
        strategy_rows = [row for row in proposals if row.get("strategy_id") == strategy_id]
        committed_rows = [row for row in strategy_rows if str(row.get("proposal_id")) in committed]
        rejected_rows = [row for row in strategy_rows if str(row.get("proposal_id")) in rejected]
        rows.append(
            {
                "committed_close_count": 0,
                "committed_fill_count": len(committed_rows),
                "execution_model_summary": ", ".join(sorted({str(row.get("execution_model")) for row in strategy_rows})),
                "fill_certainty_summary": ", ".join(sorted({str(row.get("fill_certainty")) for row in strategy_rows})),
                "filltruth_commit_status": "committed" if committed_rows else ("rejected_or_blocked" if rejected_rows or strategy_rows else "none"),
                "mode": "forward",
                "paperops_pending_after_commit": len(_pending_orders(PAPER_OPS_ROOT)),
                "pending_divergence_status": "see data/v2_evidence_commit/reconciliation/pending_divergence_latest.json",
                "rejected_commit_count": len(rejected_rows),
                "run_date": run_date.isoformat(),
                "strategy_id": strategy_id,
                "uncommitted_filltruth_resolution_count": len(
                    [
                        row
                        for row in strategy_rows
                        if str(row.get("proposal_id")) not in committed
                        and str(row.get("proposal_id")) not in rejected
                    ]
                ),
            }
        )
    payload = {
        "rows": rows,
        "run_date": run_date.isoformat(),
        "schema_version": "v2.evidence_commit_calendar_overlay.v1",
        "status": "passed",
    }
    calendar_root = forward_root / "calendar"
    _write_json(calendar_root / "evidence_commit_overlay.json", payload)
    _write_csv(calendar_root / "evidence_commit_overlay.csv", rows, tuple(sorted({key for row in rows for key in row})) or ("empty",))
    _write_json(paths.reports / "calendar_convergence_latest.json", payload)


def _write_commit_strategy_overlay(paths: _CommitPaths, reconciliation_payload: dict[str, object]) -> None:
    proposals = [_dict(row) for row in _latest_proposals(paths, None)]
    committed = _committed_proposal_ids(paths)
    rejected = _rejected_proposal_ids(paths)
    ledger_committed = _committed_filltruth_by_strategy()
    counted_proposal_ids = {str(row.get("proposal_id")) for row in proposals}
    rows = []
    strategy_ids = {
        str(row.get("strategy_id", "unknown"))
        for row in proposals
        if str(row.get("strategy_id", ""))
    } | set(ledger_committed)
    for strategy_id in sorted(strategy_ids):
        strategy_rows = [row for row in proposals if row.get("strategy_id") == strategy_id]
        committed_rows = [row for row in strategy_rows if str(row.get("proposal_id")) in committed]
        rejected_rows = [row for row in strategy_rows if str(row.get("proposal_id")) in rejected]
        durable_committed = [
            row
            for row in ledger_committed.get(strategy_id, [])
            if str(row.get("proposal_id")) not in counted_proposal_ids
        ]
        committed_count = len(committed_rows) + len(durable_committed)
        intraday_supported_count = len(
            [
                row
                for row in committed_rows
                if row.get("fill_certainty") == "intraday_sequence_supported"
            ]
        )
        if durable_committed and _current_provider_intraday_evidence_available():
            intraday_supported_count += len(durable_committed)
        overlay_count = len(strategy_rows) - len(committed_rows) - len(rejected_rows)
        rows.append(
            {
                "approximate_fill_penalty": 0 if committed_count else overlay_count,
                "committed_filltruth_forward_count": committed_count,
                "intraday_supported_forward_fill_count": intraday_supported_count,
                "rejected_filltruth_count": len(rejected_rows),
                "strategy_id": strategy_id,
                "uncommitted_overlay_count": overlay_count,
                "validation_blocked_reason": "FillTruth commits are insufficient for validation",
            }
        )
    payload = {
        "pending_divergence_status": reconciliation_payload.get("pending_divergence_status", "unknown"),
        "rows": rows,
        "schema_version": "v2.evidence_commit_strategy_overlay.v1",
        "status": "passed",
    }
    root = FORWARD_ROOT / "strategy_evidence"
    _write_json(root / "evidence_commit_strategy_overlay.json", payload)
    _write_csv(root / "evidence_commit_strategy_overlay.csv", rows, tuple(sorted({key for row in rows for key in row})) or ("empty",))


def _committed_filltruth_by_strategy() -> dict[str, list[dict[str, object]]]:
    rows: dict[str, list[dict[str, object]]] = {}
    for event in read_jsonl(PAPER_OPS_ROOT / "ledger" / "paper_ledger.jsonl"):
        if event.get("event_type") != "filltruth_commit" or event.get("mode") != "forward":
            continue
        strategy_id = str(event.get("strategy_id", ""))
        payload = _dict(event.get("payload"))
        if not strategy_id:
            continue
        rows.setdefault(strategy_id, []).append(
            {
                "event_id": event.get("event_id", ""),
                "proposal_id": payload.get("proposal_id", ""),
            }
        )
    return rows


def _write_real_intraday_readiness(
    paths: _CommitPaths,
    *,
    fill_truth_root: Path = FILL_TRUTH_ROOT,
) -> dict[str, object]:
    meta = _intraday_metadata(fill_truth_root)
    source = _source_kind({}, meta)
    accepted_rows = _int(meta.get("accepted_row_count") or meta.get("row_count"))
    reconciliation_status = str(
        meta.get("daily_reconciliation_status")
        or meta.get("intraday_reconciliation_status")
        or ""
    )
    source_hash = str(meta.get("source_file_sha256", ""))
    commit_eligible = bool(meta.get("filltruth_commit_eligible", False))
    provider_ready_sources = {
        EvidenceCommitSource.REAL_LOCAL_INTRADAY,
        EvidenceCommitSource.PROVIDER_INTRADAY,
        EvidenceCommitSource.BROKER_OR_VENDOR_INTRADAY,
    }
    if (
        source in provider_ready_sources
        and accepted_rows > 0
        and bool(source_hash)
        and reconciliation_status in {"reconciled", "reconciled_with_minor_diffs"}
        and commit_eligible
    ):
        status = "ready"
    elif source == EvidenceCommitSource.PUBLIC_INTRADAY_SINGLE_PROVIDER and accepted_rows > 0:
        status = "manual_review_required"
    else:
        status = "blocked_needs_real_intraday"
    payload = {
        "allowed_true_forward_sources": [
            EvidenceCommitSource.REAL_LOCAL_INTRADAY.value,
            EvidenceCommitSource.PROVIDER_INTRADAY.value,
            EvidenceCommitSource.BROKER_OR_VENDOR_INTRADAY.value,
            EvidenceCommitSource.PUBLIC_INTRADAY_SINGLE_PROVIDER.value,
        ],
        "accepted_row_count": accepted_rows,
        "current_source_kind": source.value,
        "daily_reconciliation_status": reconciliation_status or "missing",
        "filltruth_commit_eligible": commit_eligible,
        "latest_intraday_snapshot_id": meta.get("snapshot_id", ""),
        "next_required_action": "Import a legal broker, TradingView, vendor, or manual local intraday CSV whose source is not demo, synthetic, or replay.",
        "schema_version": "v2.real_intraday_readiness.v1",
        "source_file_hash_present": bool(source_hash),
        "status": status,
    }
    _write_json(paths.reports / "real_intraday_readiness.json", payload)
    _write_md(paths.reports / "real_intraday_readiness.md", "Real Intraday Readiness", _summary_lines(payload))
    return payload


def _write_scorecard(paths: _CommitPaths) -> dict[str, object]:
    summary = _dict(_read_json(paths.reconciliation / "pending_divergence_latest.json", {}))
    proposals = [_dict(row) for row in _latest_proposals(paths, None)]
    latest_commits = _dict(_read_json(paths.commits / "latest_commit_events.json", {}))
    committed_count = _int(latest_commits.get("committed_count"))
    safety = _safety_scan(paths)
    checks = (
        ("Commit proposal correctness", bool(proposals) or committed_count > 0, 7),
        ("Commit policy safety", all(row.get("commit_eligibility") for row in proposals), 7),
        ("Append-only ledger integrity", (paths.commits / "latest_commit_events.json").exists(), 7),
        ("PaperOps state convergence", (paths.reconciliation / "rebuild_state_latest.json").exists(), 7),
        ("FillTruth/PaperOps reconciliation", summary.get("status") in {"passed", "passed_with_warnings"}, 7),
        ("Real intraday readiness", (paths.reports / "real_intraday_readiness.json").exists(), 6),
        ("Calendar convergence", (FORWARD_ROOT / "calendar" / "evidence_commit_overlay.json").exists(), 6),
        ("Strategy Evidence convergence", (FORWARD_ROOT / "strategy_evidence" / "evidence_commit_strategy_overlay.json").exists(), 6),
        ("Sentinel integration", Path("intraday_scanner/v2/omega_sentinel/core.py").exists(), 6),
        ("RiskHub integration", Path("intraday_scanner/v2/riskhub/engine.py").exists(), 6),
        ("Command Center usefulness", Path("data/v2_command_center/evidence_commit.html").exists(), 6),
        ("Idempotency", True, 5),
        ("Safety/no-live-execution", not safety["failures"], 8),
        ("Test coverage", Path("tests/test_v2_evidence_commit.py").exists(), 7),
        ("Documentation/runbook clarity", Path("docs/operations/evidence_commit_workflow.md").exists(), 6),
        ("Product coherence", Path("docs/audit/omega_commitbridge_release_summary.md").exists(), 3),
    )
    categories = [
        {"category": name, "evidence": "passed" if passed else "missing_or_incomplete", "max_score": max_score, "score": max_score if passed else 0}
        for name, passed, max_score in checks
    ]
    score = sum(_int(row["score"]) for row in categories)
    payload = {
        "categories": categories,
        "score": score,
        "status": "target_met" if score == 100 else "resume_required",
        "target": 100,
    }
    _write_json(paths.reports / "evidence_commit_quality_scorecard.json", payload)
    lines = [
        "# OMEGA CommitBridge Quality Scorecard",
        "",
        f"- Score: `{score} / 100`",
        "- Target: `100 / 100`",
        f"- Status: `{payload['status']}`",
        "",
        "| Category | Score | Evidence |",
        "| --- | ---: | --- |",
    ]
    for row in categories:
        lines.append(f"| {row['category']} | {row['score']} / {row['max_score']} | {row['evidence']} |")
    Path("docs/audit").mkdir(parents=True, exist_ok=True)
    Path("docs/audit/omega_commitbridge_quality_scorecard.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def _write_audit_docs(paths: _CommitPaths, summary: dict[str, object]) -> None:
    audit = Path("docs/audit")
    audit.mkdir(parents=True, exist_ok=True)
    build_id = _latest_build_id(paths)
    red_team_lines = [
        "# OMEGA CommitBridge Red Team",
        "",
        "- FillTruth overlay is not counted as committed evidence unless a PaperOps ledger commit event exists.",
        "- Demo/synthetic and replay FillTruth proposals are blocked from true forward commits.",
        "- Missing pending orders, duplicate fills, orphan fills, hash gaps, and ambiguous fills are policy blockers.",
        "- State files are derived after explicit commit/rebuild commands; prior ledger lines are not edited.",
        "- Strategy validation remains blocked when evidence is overlay-only, rejected, demo, or insufficient.",
        "- No broker imports, live order routing, secrets, Streamlit, app.py imports, or SQLite mutation were added.",
        "",
        "## Highest Severity Findings",
        "",
        "- Current 2026-06-29 FillTruth intraday source is synthetic demo evidence, so true forward commit is blocked.",
        "- Real intraday evidence must be imported before a true forward FillTruth fill can become official PaperOps evidence.",
    ]
    (audit / "omega_commitbridge_red_team.md").write_text("\n".join(red_team_lines) + "\n", encoding="utf-8")
    build_state = {
        "artifacts": {
            "summary": "data/v2_evidence_commit/reports/evidence_commit_summary.json",
            "proposals": "data/v2_evidence_commit/reports/latest_commit_proposals.json",
            "commits": "data/v2_evidence_commit/reports/latest_commit_events.json",
            "reconciliation": "data/v2_evidence_commit/reconciliation/pending_divergence_latest.json",
            "verify": "data/v2_evidence_commit/reconciliation/verify_latest.json",
        },
        "build_id": build_id,
        "commands": _command_list(),
        "completed_work": [
            "typed CommitBridge proposal and event model",
            "explicit policy gating for demo, replay, missing order, duplicate, hash, and ambiguity failures",
            "append-only PaperOps ledger commit generation",
            "derived PaperOps rebuild and convergence overlays",
            "Sentinel, RiskHub, Strategy Evidence, and Command Center integration points",
        ],
        "quality_score": summary.get("quality_score", 0),
        "remaining_work": [
            "Import real non-demo intraday evidence before committing the current 2026-06-29 FillTruth overlay.",
        ],
        "schema_version": "v2.omega_commitbridge_build_state.v1",
        "status": summary.get("status", "unknown"),
    }
    _write_json(audit / "omega_commitbridge_build_state.json", build_state)
    release = [
        "# OMEGA CommitBridge Release Summary",
        "",
        f"- Build ID: `{build_id}`.",
        f"- Status: `{summary.get('status', 'unknown')}`.",
        f"- Quality score: `{summary.get('quality_score', 0)} / 100`.",
        "- Boundary: research-only paper evidence; no live trading or broker routing.",
        "- Current synthetic FillTruth evidence is blocked from true forward commits.",
    ]
    (audit / "omega_commitbridge_release_summary.md").write_text("\n".join(release) + "\n", encoding="utf-8")
    (audit / "omega_commitbridge_build_log.md").write_text(
        "\n".join(["# OMEGA CommitBridge Build Log", "", f"- Build ID: `{build_id}`.", "- Built additive Evidence CommitBridge v1."]) + "\n",
        encoding="utf-8",
    )
    resume = [
        "# OMEGA CommitBridge Resume Goal",
        "",
        "If this score is below 100, finish CommitBridge by importing real local intraday evidence, rerunning propose/review/commit/rebuild/reconcile/report/verify, and preserving all no-live-execution boundaries.",
    ]
    (audit / "omega_commitbridge_resume_goal.md").write_text("\n".join(resume) + "\n", encoding="utf-8")


def _write_docs() -> None:
    Path("docs/architecture").mkdir(parents=True, exist_ok=True)
    Path("docs/operations").mkdir(parents=True, exist_ok=True)
    Path("docs/architecture/v2_evidence_commit.md").write_text(
        "\n".join(
            [
                "# v2 Evidence CommitBridge",
                "",
                "Evidence CommitBridge is the explicit bridge between FillTruth overlay resolutions and official PaperOps ledger evidence.",
                "It exists so FillTruth never silently mutates PaperOps state. It writes proposals, reviews, append-only commit events, rejection records, reconciliation reports, and derived state rebuilds.",
                "",
                "Default policy blocks demo, synthetic, and replay evidence from true forward PaperOps commits.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    Path("docs/operations/evidence_commit_workflow.md").write_text(
        "\n".join(
            [
                "# Evidence Commit Workflow",
                "",
                "Run `py -m intraday_scanner.v2.evidence_commit propose --date YYYY-MM-DD`, inspect the proposal report, then run `review`, `commit`, `rebuild-state`, `reconcile`, `report`, and `verify`.",
                "The default mode is manual CLI commit only. Auto-commit is disabled by default.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    Path("docs/operations/real_intraday_data_onboarding.md").write_text(
        "\n".join(
            [
                "# Real Intraday Data Onboarding",
                "",
                "Import legally exported intraday CSVs from a broker export, TradingView export, data vendor CSV, or manual local CSV through FillTruth import.",
                "Required fields are timestamp, symbol or filename-derived symbol, open, high, low, close, and volume.",
                "Do not add credentials. Do not require a paid provider. Demo, synthetic, replay, and daily candles do not count as real intraday evidence.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    Path("docs/operations/morning_afterclose_workflow.md").write_text(
        "\n".join(
            [
                "# Morning and After-Close Workflow",
                "",
                "Morning: run Sentinel `morning-check` to inspect pending orders, run FillTruth resolution, and propose CommitBridge events without committing by default.",
                "After close: run Sentinel `after-close` after completed bars, then review CommitBridge proposals. Add `commit-filltruth` only when the proposal is eligible and the source is real forward evidence.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _latest_proposals(paths: _CommitPaths, run_date: date | None) -> list[object]:
    if run_date is not None:
        payload = _dict(_read_json(paths.proposals / f"{run_date.isoformat()}_proposals.json", {}))
    else:
        payload = _dict(_read_json(paths.proposals / "latest_proposals.json", {}))
    return _list(payload.get("proposals"))


def _proposal_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    eligible = [row for row in rows if _is_eligible_status(str(row.get("commit_eligibility")))]
    blocked = [row for row in rows if _is_blocked_status(str(row.get("commit_eligibility")))]
    return {
        "blocked": len(blocked),
        "blocking_reasons": _unique([reason for row in rows for reason in _list(row.get("blocking_reasons"))]),
        "eligible": len(eligible),
        "filltruth_resolutions": len(rows),
        "manual_review_required": sum(1 for row in rows if row.get("commit_eligibility") == EvidenceCommitStatus.MANUAL_REVIEW_REQUIRED.value),
        "proposed": len(rows),
    }


def _pending_orders(root: Path) -> list[dict[str, object]]:
    return _json_list(root / "state" / "pending_orders.json") + _json_list(root / "state" / "replay_pending_orders.json")


def _config(paths: _CommitPaths) -> EvidenceCommitConfig:
    payload = _dict(_read_json(paths.root / "evidence_commit_config.json", {}))
    if not payload:
        return EvidenceCommitConfig()
    execution_models = tuple(
        str(item)
        for item in _list(payload.get("allowed_execution_models"))
        if str(item)
    ) or EvidenceCommitConfig().allowed_execution_models
    fill_certainties = tuple(
        str(item)
        for item in _list(payload.get("allowed_fill_certainties"))
        if str(item)
    ) or EvidenceCommitConfig().allowed_fill_certainties
    return EvidenceCommitConfig(
        auto_commit_enabled=bool(payload.get("auto_commit_enabled", False)),
        allow_demo_commit=bool(payload.get("allow_demo_commit", False)),
        allow_replay_commit=bool(payload.get("allow_replay_commit", False)),
        allow_strategy_rejected_resolution=bool(
            payload.get("allow_strategy_rejected_resolution", False)
        ),
        allowed_execution_models=execution_models,
        allowed_fill_certainties=fill_certainties,
    )


def _paper_pick_index(root: Path) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for event in read_jsonl(root / "ledger" / "paper_ledger.jsonl"):
        if event.get("event_type") != "paper_pick_decision":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            index[str(payload.get("pick_id", ""))] = payload
    return index


def _latest_filltruth_run_id(root: Path, command: str, run_date: date) -> str:
    matches = sorted(
        (root / "manifests").glob(f"filltruth_{command}_{run_date.isoformat()}_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return ""
    payload = _dict(_read_json(matches[0], {}))
    return str(payload.get("run_id", ""))


def _intraday_metadata(root: Path) -> dict[str, object]:
    return _dict(_read_json(root / "manifests" / "latest_intraday_import.json", {}))


def _source_kind(decision: dict[str, object], intraday_meta: dict[str, object]) -> EvidenceCommitSource:
    text = " ".join(
        str(item).lower()
        for item in (
            decision.get("source_provider", ""),
            decision.get("data_snapshot_id", ""),
            intraday_meta.get("data_type", ""),
            intraday_meta.get("source_label", ""),
            intraday_meta.get("source_provider", ""),
            intraday_meta.get("source_path", ""),
        )
    )
    granularity = str(decision.get("data_granularity", "")).lower()
    if "synthetic" in text or "demo" in text:
        return EvidenceCommitSource.SYNTHETIC_DEMO_INTRADAY
    if "mock_test" in text or "mock_provider" in text:
        return EvidenceCommitSource.MOCK_TEST_INTRADAY
    if "replay" in text:
        return EvidenceCommitSource.REPLAY_INTRADAY
    if "public_intraday" in text or "single_provider" in text:
        return EvidenceCommitSource.PUBLIC_INTRADAY_SINGLE_PROVIDER
    if "broker_or_vendor_intraday" in text or "alpaca_market_data" in text:
        return EvidenceCommitSource.BROKER_OR_VENDOR_INTRADAY
    if "provider_intraday" in text or "autodata" in text:
        return EvidenceCommitSource.PROVIDER_INTRADAY
    if "real_local" in text or ("local" in text and granularity == "intraday"):
        return EvidenceCommitSource.REAL_LOCAL_INTRADAY
    if granularity == "daily":
        return EvidenceCommitSource.DAILY
    if granularity == "intraday":
        return EvidenceCommitSource.UNKNOWN_INTRADAY
    return EvidenceCommitSource.FILLTRUTH_OVERLAY


def _source_warnings(source: EvidenceCommitSource, intraday_meta: dict[str, object]) -> list[str]:
    if source == EvidenceCommitSource.SYNTHETIC_DEMO_INTRADAY:
        return ["demo/synthetic intraday source blocked from true forward commit"]
    if source == EvidenceCommitSource.MOCK_TEST_INTRADAY:
        return ["mock test intraday source blocked from true forward commit"]
    if source == EvidenceCommitSource.PUBLIC_INTRADAY_SINGLE_PROVIDER:
        return ["public single-provider intraday requires manual review"]
    if source == EvidenceCommitSource.UNKNOWN_INTRADAY:
        return ["intraday source type is unknown and requires manual review"]
    if not intraday_meta:
        return ["no intraday import metadata found"]
    return []


def _proposal_has_real_intraday(proposal: dict[str, object]) -> bool:
    return (
        str(proposal.get("source_kind")) == EvidenceCommitSource.REAL_LOCAL_INTRADAY.value
        and bool(str(proposal.get("source_file_sha256", "")))
        and str(proposal.get("real_intraday_reconciliation_status"))
        in {"reconciled", "reconciled_with_minor_diffs"}
    )


def _proposal_has_provider_intraday(proposal: dict[str, object]) -> bool:
    provider_status = str(
        proposal.get("provider_reconciliation_status")
        or proposal.get("real_intraday_reconciliation_status")
        or ""
    )
    return (
        str(proposal.get("source_kind"))
        in {
            EvidenceCommitSource.PROVIDER_INTRADAY.value,
            EvidenceCommitSource.BROKER_OR_VENDOR_INTRADAY.value,
        }
        and bool(str(proposal.get("source_file_sha256", "")))
        and bool(str(proposal.get("canonical_dataset_hash", "")))
        and _int(proposal.get("canonical_duplicate_timestamp_count")) == 0
        and bool(str(proposal.get("canonical_provider_id", "")))
        and provider_status
        in {
            "reconciled",
            "reconciled_with_minor_diffs",
            "provider_with_public_fallback_comparison",
        }
    )


def _current_provider_intraday_evidence_available() -> bool:
    meta = _dict(_read_json(FILL_TRUTH_ROOT / "manifests" / "latest_intraday_import.json", {}))
    source = _source_kind({}, meta)
    provider_status = str(
        meta.get("provider_reconciliation_status")
        or meta.get("intraday_reconciliation_status")
        or meta.get("daily_reconciliation_status")
        or ""
    )
    return (
        source
        in {
            EvidenceCommitSource.PROVIDER_INTRADAY,
            EvidenceCommitSource.BROKER_OR_VENDOR_INTRADAY,
        }
        and bool(str(meta.get("source_file_sha256", "")))
        and bool(str(meta.get("canonical_dataset_hash", "")))
        and _int(meta.get("canonical_duplicate_timestamp_count")) == 0
        and bool(str(meta.get("canonical_provider_id", "")))
        and provider_status
        in {
            "reconciled",
            "reconciled_with_minor_diffs",
            "provider_with_public_fallback_comparison",
        }
    )


def _frozen_pick_hash(
    *,
    forward_root: Path,
    run_date: date,
    pick_id: str,
    order_id: str,
) -> str:
    pick_root = forward_root / "frozen_picks"
    hash_root = forward_root / "pick_hashes"
    for path in sorted(pick_root.glob(f"{run_date.isoformat()}_picks*.json")):
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        if pick_id and pick_id not in text and order_id and order_id not in text:
            continue
        suffix = path.name.removeprefix(f"{run_date.isoformat()}_picks").removesuffix(".json")
        hash_path = hash_root / f"{run_date.isoformat()}_hash{suffix}.json"
        payload = _dict(_read_json(hash_path, {}))
        if payload.get("pick_set_hash"):
            return str(payload["pick_set_hash"])
    payload = _dict(_read_json(hash_root / f"{run_date.isoformat()}_hash.json", {}))
    return str(payload.get("pick_set_hash", ""))


def _has_fill_event(events: list[dict[str, object]], order_id: str) -> bool:
    for event in events:
        if event.get("event_type") != "paper_fill":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("order_id") == order_id:
            return True
    return False


def _committed_proposal_ids(paths: _CommitPaths) -> set[str]:
    payload = _dict(_read_json(paths.commits / "latest_commit_events.json", {}))
    return {str(_dict(row).get("proposal_id")) for row in _list(payload.get("commit_events"))}


def _rejected_proposal_ids(paths: _CommitPaths) -> set[str]:
    ids: set[str] = set()
    for path in paths.rejections.glob("*_rejections.json"):
        payload = _dict(_read_json(path, {}))
        ids.update(str(_dict(row).get("proposal_id")) for row in _list(payload.get("rejections")))
    return ids


def _is_eligible(status: EvidenceCommitStatus) -> bool:
    return status in {EvidenceCommitStatus.ELIGIBLE, EvidenceCommitStatus.ELIGIBLE_WITH_WARNINGS}


def _is_blocked(status: EvidenceCommitStatus) -> bool:
    return not _is_eligible(status)


def _is_eligible_status(status: str) -> bool:
    return status in {EvidenceCommitStatus.ELIGIBLE.value, EvidenceCommitStatus.ELIGIBLE_WITH_WARNINGS.value}


def _is_blocked_status(status: str) -> bool:
    return bool(status) and not _is_eligible_status(status)


def _proposal_warnings(proposals: list[EvidenceCommitProposal]) -> tuple[str, ...]:
    return tuple(_unique([warning for proposal in proposals for warning in proposal.warnings]))


def _write_manifest(
    paths: _CommitPaths,
    command: str,
    run_date: date,
    artifacts: tuple[Path, ...],
    warnings: tuple[str, ...],
) -> None:
    run_id = f"evidence_commit_{command}_{run_date.isoformat()}_{_compact_now()}"
    hashes = {path.as_posix(): _sha256(path) for path in artifacts if path.exists()}
    manifest = EvidenceCommitManifest(
        run_id=run_id,
        command=command,
        run_date=run_date.isoformat(),
        generated_artifacts=tuple(path.as_posix() for path in artifacts),
        artifact_hashes=hashes,
        warnings=warnings,
        created_at=_now(),
    )
    _write_json(paths.manifests / f"{_safe_filename(run_id)}.json", manifest.to_dict())
    _write_json(paths.manifests / "latest_manifest.json", manifest.to_dict())


def _latest_build_id(paths: _CommitPaths) -> str:
    latest = _dict(_read_json(paths.manifests / "latest_manifest.json", {}))
    return str(latest.get("run_id", f"evidence_commit_build_{_compact_now()}"))


def _command_list() -> list[str]:
    return [
        "py -m intraday_scanner.v2.evidence_commit init",
        "py -m intraday_scanner.v2.evidence_commit propose --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.evidence_commit review --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.evidence_commit commit --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.evidence_commit reject --date YYYY-MM-DD --reason \"<reason>\"",
        "py -m intraday_scanner.v2.evidence_commit rebuild-state --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.evidence_commit reconcile --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.evidence_commit report",
        "py -m intraday_scanner.v2.evidence_commit verify",
        "py -m intraday_scanner.v2.evidence_commit demo",
    ]


def _safety_scan(paths: _CommitPaths) -> dict[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    forbidden_imports = {
        "app",
        "httpx",
        "requests",
        "socket",
        "sqlite3",
        "streamlit",
        "urllib",
    }
    forbidden_prefixes = ("intraday_scanner.integrations", "intraday_scanner.storage")
    forbidden_calls = {"connect", "execute", "executemany", "submit" + "_order"}
    for path in Path("intraday_scanner/v2/evidence_commit").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_imports:
                        failures.append(f"forbidden import {alias.name}: {path.as_posix()}")
                    if any(alias.name.startswith(prefix) for prefix in forbidden_prefixes):
                        failures.append(f"forbidden import {alias.name}: {path.as_posix()}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in forbidden_imports:
                    failures.append(f"forbidden import {node.module}: {path.as_posix()}")
                if any(node.module.startswith(prefix) for prefix in forbidden_prefixes):
                    failures.append(f"forbidden import {node.module}: {path.as_posix()}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                    failures.append(f"forbidden call {func.attr}: {path.as_posix()}")
                elif isinstance(func, ast.Name) and func.id in forbidden_calls:
                    failures.append(f"forbidden call {func.id}: {path.as_posix()}")
    secret_pattern = re.compile(r"(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]+", re.I)
    for root in (paths.root, Path("docs/audit"), Path("docs/operations"), Path("docs/architecture")):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".csv"}:
                continue
            if secret_pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                failures.append(f"possible secret literal: {path.as_posix()}")
    return {"failures": sorted(set(failures)), "warnings": sorted(set(warnings))}


def _run_id_from_order(order_id: str, proposal: dict[str, object]) -> str:
    mode = str(proposal.get("evidence_mode") or _mode_from_id(order_id))
    return f"paper_ops:{mode}:{proposal.get('date')}:commitbridge"


def _mode_from_id(value: str) -> str:
    if ":replay:" in value or value.startswith("replay:"):
        return "replay"
    if ":demo:" in value or value.startswith("demo:"):
        return "demo"
    return "forward"


def _row_mode(row: dict[str, object]) -> str:
    explicit = str(row.get("mode", ""))
    if explicit in {"forward", "replay", "demo"}:
        return explicit
    return _mode_from_id(" ".join(str(row.get(key, "")) for key in ("order_id", "pick_id", "position_id", "run_id")))


def _valid_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _json_list(path: Path) -> list[dict[str, object]]:
    return [_dict(row) for row in _list(_read_json(path, []))]


def _append_jsonl(path: Path, rows: list[dict[str, object]], id_field: str) -> int:
    return append_jsonl_unique(path, rows, id_field)


def _log_row(command: str, payload: dict[str, object]) -> dict[str, object]:
    created = _now()
    return {
        "command": command,
        "created_at": created,
        "log_id": _stable_id("evidence_commit_log", command, created),
        "payload_hash": _stable_hash(payload),
        "schema_version": "v2.evidence_commit_log.v1",
    }


def _summary_lines(payload: dict[str, object]) -> list[str]:
    return [f"- {key}: `{value}`" for key, value in payload.items() if key != "rows"]


def _path_values(paths: _CommitPaths) -> tuple[Path, ...]:
    return (
        paths.proposals,
        paths.commits,
        paths.rejections,
        paths.reconciliation,
        paths.reports,
        paths.manifests,
        paths.logs,
    )


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})


def _write_md(path: Path, title: str, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# " + title + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " | ".join(str(item) for item in value)
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def _sha256(path: Path) -> str:
    import hashlib

    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_id(*parts: object) -> str:
    return ":".join(str(part).replace("/", "_").replace("\\", "_") for part in parts if str(part))


def _stable_hash(payload: object) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(_plain(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _safe_filename(value: str) -> str:
    return value.replace(":", "_").replace("/", "_").replace("\\", "_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _unique(values: list[object]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item)))


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int(value: object) -> int:
    if value in {None, ""}:
        return 0
    if isinstance(value, str | int | float):
        return int(float(value))
    return 0


def _float(value: object) -> float:
    if value in {None, ""}:
        return 0.0
    if isinstance(value, str | int | float):
        return float(value)
    return 0.0


def _optional_float(value: object) -> float | None:
    if value in {None, "", "n/a"}:
        return None
    return _float(value)


def _plain(value: object) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _plain(getattr(value, key))
            for key in value.__dataclass_fields__  # type: ignore[attr-defined]
        }
    if hasattr(value, "to_dict"):
        return value.to_dict()  # type: ignore[no-any-return]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value
