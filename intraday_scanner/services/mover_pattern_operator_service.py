"""Scheduled, paper-only operations for the Mover Pattern Lab.

The service is deliberately thin: source and outcome truth stay in
``intraday_scanner.v2.mover_pattern_lab.core``.  This module resolves configured
retained inputs, invokes the public core APIs, records per-cutoff operator state,
and delivers a durable research notification.  It never talks to a broker and
never turns missing evidence into a zero return.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from intraday_scanner.config import load_config
from intraday_scanner.market_calendar import market_session
from intraday_scanner.notifiers import NotificationEvent, build_notifiers
from intraday_scanner.notifiers.base import BaseNotifier
from intraday_scanner.notifiers.console import ConsoleNotifier
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.v2.mover_pattern_lab.core import (
    DEFAULT_OUTPUT_ROOT,
    analyze,
    build_snapshots_from_bars,
    init,
    paper_scan,
    reconcile_paper_signals,
    verify,
)
from intraday_scanner.v2.paper_ops.storage import exclusive_file_lock, write_json

MARKET_TZ = ZoneInfo("America/New_York")
WORKFLOW_SCHEMA = "dawnstrike.mover_daily_workflow.v1"
OPERATOR_SCHEMA = "dawnstrike.mover_daily_operator.v1"
OPERATOR_RECEIPT_SCHEMA = "dawnstrike.mover_daily_operator_receipt.v1"
RECONCILIATION_RECEIPT_SCHEMA = "dawnstrike.mover_reconciliation_receipt.v1"
_CHANNELS = frozenset({"console", "telegram"})
_SUCCESS_STATUSES = frozenset({"passed", "passed_no_signal"})
_REGULAR_CLOSE = time(16, 0)
_MAX_COMPACT_MESSAGE_CHARS = 850


@dataclass(frozen=True)
class MoverWorkflowInputs:
    """Paths returned by a retained-input adapter for one market session."""

    bars_csv: Path
    context_csv: Path | None = None


class MoverWorkflowInputAdapter(Protocol):
    """Boundary for genuine, already-retained market inputs."""

    def scan_inputs(self, *, market_date: str, cutoff_et: str) -> MoverWorkflowInputs:
        """Return bars and point-in-time context retained for a cutoff."""

    def reconciliation_inputs(self, *, market_date: str) -> MoverWorkflowInputs:
        """Return complete after-close bars for paper reconciliation."""


@dataclass(frozen=True)
class MoverDailyWorkflowConfig:
    """Strict operator configuration; it contains paths, never market data."""

    config_path: Path
    bars_csv_template: str
    context_csv_template: str
    reconciliation_bars_csv_template: str
    cutoffs_et: tuple[str, ...]
    reconcile_not_before_et: str
    output_root: Path
    notification_db_path: Path
    env_file: Path
    notification_channel: str
    min_baseline_sessions: int
    bar_interval_minutes: int
    notional_per_trade: float
    slippage_bps: float
    fee_bps: float

    @classmethod
    def load(cls, path: str | Path) -> MoverDailyWorkflowConfig:
        config_path = Path(path).resolve()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("mover daily workflow config must be a JSON object")
        if str(raw.get("schema_version") or "") != WORKFLOW_SCHEMA:
            raise ValueError(f"schema_version must be {WORKFLOW_SCHEMA!r}")
        if str(raw.get("input_adapter") or "") != "retained_csv":
            raise ValueError("input_adapter must be 'retained_csv'")
        if raw.get("research_only") is not True:
            raise ValueError("mover daily workflow must explicitly set research_only=true")
        if raw.get("broker_execution_enabled") is not False:
            raise ValueError(
                "mover daily workflow must explicitly set broker_execution_enabled=false"
            )
        base = config_path.parent
        cutoffs = tuple(str(value).strip() for value in raw.get("cutoffs_et") or ())
        if not cutoffs:
            raise ValueError("cutoffs_et must contain at least one ET cutoff")
        reconcile_not_before = str(raw.get("reconcile_not_before_et") or "16:10")
        for value in (*cutoffs, reconcile_not_before):
            _clock(value)
        if _clock(reconcile_not_before) < _REGULAR_CLOSE:
            raise ValueError(
                "reconcile_not_before_et cannot precede the regular 16:00 ET close"
            )
        if len(cutoffs) != len(set(cutoffs)):
            raise ValueError("cutoffs_et cannot contain duplicates")
        channel = str(raw.get("notification_channel") or "telegram").lower()
        if channel not in _CHANNELS:
            raise ValueError("notification_channel must be telegram or console")
        min_baseline = int(raw.get("min_baseline_sessions", 10))
        interval = int(raw.get("bar_interval_minutes", 5))
        notional = float(raw.get("notional_per_trade", 1000.0))
        slippage = float(raw.get("slippage_bps", 10.0))
        fees = float(raw.get("fee_bps", 1.0))
        if min_baseline < 1:
            raise ValueError("min_baseline_sessions must be positive")
        if interval < 1 or interval > 30:
            raise ValueError("bar_interval_minutes must be between 1 and 30")
        if notional <= 0 or slippage < 0 or fees < 0:
            raise ValueError("notional must be positive and costs cannot be negative")

        def configured_path(field: str, default: str) -> Path:
            candidate = Path(str(raw.get(field) or default))
            return candidate if candidate.is_absolute() else (base / candidate).resolve()

        output_root = configured_path("output_root", str(DEFAULT_OUTPUT_ROOT))
        notification_db = configured_path(
            "notification_db_path", "data/mover_pattern_notifications.sqlite"
        )
        env_file = configured_path("env_file", ".env")
        bars_template = _required_text(raw, "bars_csv_template")
        context_template = _required_text(raw, "context_csv_template")
        reconcile_template = str(
            raw.get("reconciliation_bars_csv_template") or bars_template
        ).strip()
        return cls(
            config_path=config_path,
            bars_csv_template=bars_template,
            context_csv_template=context_template,
            reconciliation_bars_csv_template=reconcile_template,
            cutoffs_et=cutoffs,
            reconcile_not_before_et=reconcile_not_before,
            output_root=output_root,
            notification_db_path=notification_db,
            env_file=env_file,
            notification_channel=channel,
            min_baseline_sessions=min_baseline,
            bar_interval_minutes=interval,
            notional_per_trade=notional,
            slippage_bps=slippage,
            fee_bps=fees,
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": WORKFLOW_SCHEMA,
            "config_path": str(self.config_path),
            "input_adapter": "retained_csv",
            "bars_csv_template": self.bars_csv_template,
            "context_csv_template": self.context_csv_template,
            "reconciliation_bars_csv_template": (
                self.reconciliation_bars_csv_template
            ),
            "cutoffs_et": list(self.cutoffs_et),
            "reconcile_not_before_et": self.reconcile_not_before_et,
            "output_root": str(self.output_root),
            "notification_db_path": str(self.notification_db_path),
            "env_file": str(self.env_file),
            "notification_channel": self.notification_channel,
            "min_baseline_sessions": self.min_baseline_sessions,
            "bar_interval_minutes": self.bar_interval_minutes,
            "notional_per_trade": self.notional_per_trade,
            "slippage_bps": self.slippage_bps,
            "fee_bps": self.fee_bps,
            "research_only": True,
            "broker_execution_enabled": False,
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.public_payload())


class RetainedCsvMoverInputAdapter:
    """Resolve configured CSV templates without fabricating or fetching data."""

    def __init__(self, config: MoverDailyWorkflowConfig):
        self.config = config

    def scan_inputs(self, *, market_date: str, cutoff_et: str) -> MoverWorkflowInputs:
        return MoverWorkflowInputs(
            bars_csv=self._resolve(
                self.config.bars_csv_template,
                market_date=market_date,
                cutoff_et=cutoff_et,
            ),
            context_csv=self._resolve(
                self.config.context_csv_template,
                market_date=market_date,
                cutoff_et=cutoff_et,
            ),
        )

    def reconciliation_inputs(self, *, market_date: str) -> MoverWorkflowInputs:
        return MoverWorkflowInputs(
            bars_csv=self._resolve(
                self.config.reconciliation_bars_csv_template,
                market_date=market_date,
                cutoff_et="after_close",
            )
        )

    def _resolve(self, template: str, *, market_date: str, cutoff_et: str) -> Path:
        rendered = template.format(
            market_date=market_date,
            cutoff_et=cutoff_et,
            cutoff_token=cutoff_et.replace(":", ""),
        )
        candidate = Path(rendered)
        if not candidate.is_absolute():
            candidate = self.config.config_path.parent / candidate
        return candidate.resolve()


def run_mover_daily_workflow(
    *,
    config_path: str | Path,
    stage: str,
    market_date: str | None = None,
    cutoff_et: str | None = None,
    notification_channel: str | None = None,
    input_adapter: MoverWorkflowInputAdapter | None = None,
    notifiers: Sequence[BaseNotifier] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one scheduled operator stage and return a scheduler-safe payload."""

    config = MoverDailyWorkflowConfig.load(config_path)
    channel = (notification_channel or config.notification_channel).strip().lower()
    if channel not in _CHANNELS:
        raise ValueError("notification channel must be telegram or console")
    observed_now = _aware_now(now)
    day = _market_date(market_date or observed_now.astimezone(MARKET_TZ).date().isoformat())
    adapter = input_adapter or RetainedCsvMoverInputAdapter(config)
    normalized_stage = stage.strip().lower()
    if normalized_stage not in {"scan", "reconcile"}:
        raise ValueError("stage must be scan or reconcile")
    if normalized_stage == "scan":
        if not cutoff_et:
            raise ValueError("scan stage requires cutoff_et")
        if cutoff_et not in config.cutoffs_et:
            raise ValueError("scan cutoff is not declared in cutoffs_et")
        state_path = _scan_state_path(config.output_root, day, cutoff_et)
    else:
        if cutoff_et:
            raise ValueError("reconcile stage does not accept cutoff_et")
        state_path = _reconcile_state_path(config.output_root, day)

    lock_path = state_path.with_suffix(".lock")
    with exclusive_file_lock(lock_path):
        prior = _read_mapping(state_path)
        prior_workflow_status = str(
            prior.get("workflow_status") or prior.get("status") or ""
        )
        prior_receipt_payload: dict[str, Any] | None = None
        if prior:
            try:
                prior_receipt_payload = _validated_operator_state_receipt(
                    prior,
                    expected_stage=normalized_stage,
                    expected_market_date=day,
                    expected_cutoff_et=cutoff_et,
                    expected_config_fingerprint=config.fingerprint,
                    expected_output_root=config.output_root,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return _blocked_without_run(
                    config=config,
                    stage=normalized_stage,
                    market_date=day,
                    cutoff_et=cutoff_et,
                    state_path=state_path,
                    channel=channel,
                    notifiers=notifiers,
                    reason="existing operator state failed immutable validation: "
                    + _safe_error(exc),
                )
        if prior and prior_workflow_status in _SUCCESS_STATUSES:
            if prior_receipt_payload is None:
                raise ValueError("validated prior operator receipt is unavailable")
            return _redeliver_prior(
                prior,
                receipt_payload=prior_receipt_payload,
                config=config,
                state_path=state_path,
                channel=channel,
                notifiers=notifiers,
            )

        try:
            if normalized_stage == "scan":
                payload = _run_scan(
                    config=config,
                    adapter=adapter,
                    market_date=day,
                    cutoff_et=str(cutoff_et),
                    now=observed_now,
                )
            else:
                payload = _run_reconciliation(
                    config=config,
                    adapter=adapter,
                    market_date=day,
                    now=observed_now,
                )
        except Exception as exc:  # Scheduler boundary must persist and notify failure.
            payload = {
                "schema_version": OPERATOR_SCHEMA,
                "status": "blocked",
                "stage": normalized_stage,
                "market_date": day,
                "cutoff_et": cutoff_et,
                "blockers": [_safe_error(exc)],
                "research_only": True,
                "broker_execution_enabled": False,
            }
        payload["config_fingerprint"] = config.fingerprint
        payload["operator_state_path"] = str(state_path.resolve())
        payload["workflow_status"] = payload["status"]
        prior_attempt_refs = [
            str(value)
            for value in prior.get("operator_attempt_receipt_refs") or ()
            if str(value)
        ]
        prior_receipt_ref = str(prior.get("operator_receipt_ref") or "")
        if prior_receipt_ref and prior_receipt_ref not in prior_attempt_refs:
            prior_attempt_refs.append(prior_receipt_ref)
        payload["prior_operator_attempt_receipt_refs"] = prior_attempt_refs
        receipt = _retain_operator_receipt(config.output_root, payload)
        state_payload = {
            **payload,
            "operator_receipt_ref": receipt["operator_receipt_ref"],
            "operator_receipt_path": receipt["operator_receipt_path"],
            "operator_receipt_content_sha256": receipt[
                "operator_receipt_content_sha256"
            ],
            "operator_receipt_file_sha256": receipt[
                "operator_receipt_file_sha256"
            ],
            "operator_attempt_receipt_refs": [
                *prior_attempt_refs,
                receipt["operator_receipt_ref"],
            ],
        }
        events = _notification_events(payload)
        event_payloads = [_event_payload(event) for event in events]
        state_payload["notification_events"] = event_payloads
        if event_payloads:
            state_payload["notification_event"] = event_payloads[0]
        state_payload["notification"] = {
            "status": "pending",
            "event_count": len(events),
        }
        state_payload["notification_status"] = "pending"
        write_json(state_path, state_payload)
        delivery = _deliver_events(
            events,
            config=config,
            channel=channel,
            notifiers=notifiers,
        )
        state_payload["notification"] = delivery
        state_payload["notification_status"] = delivery["status"]
        write_json(state_path, state_payload)
        return state_payload


def _run_scan(
    *,
    config: MoverDailyWorkflowConfig,
    adapter: MoverWorkflowInputAdapter,
    market_date: str,
    cutoff_et: str,
    now: datetime,
) -> dict[str, Any]:
    local_now = now.astimezone(MARKET_TZ)
    if local_now.date() != date.fromisoformat(market_date):
        raise ValueError("forward scan market_date must equal the current ET market date")
    cutoff_at = datetime.combine(
        date.fromisoformat(market_date), _clock(cutoff_et), tzinfo=MARKET_TZ
    )
    if local_now < cutoff_at:
        raise ValueError(f"cutoff {cutoff_et} ET has not completed")
    inputs = adapter.scan_inputs(market_date=market_date, cutoff_et=cutoff_et)
    _required_file(inputs.bars_csv, "cutoff bars CSV")
    if inputs.context_csv is None:
        raise ValueError("forward scan requires a retained context CSV")
    _required_file(inputs.context_csv, "cutoff context CSV")
    init(output_root=config.output_root)
    snapshot_build = build_snapshots_from_bars(
        bars_csv=inputs.bars_csv,
        context_csv=inputs.context_csv,
        market_date=market_date,
        cutoffs=(cutoff_et,),
        min_baseline_sessions=config.min_baseline_sessions,
        bar_interval_minutes=config.bar_interval_minutes,
        bar_timestamp_semantics="bar_close",
        evidence_mode="forward_observation",
        output_root=config.output_root,
    )
    snapshot_path = Path(str(snapshot_build.get("snapshot_path") or ""))
    _required_file(snapshot_path, "retained snapshot ledger")
    scan = paper_scan(
        snapshots_path=snapshot_path,
        expected_market_dates=(market_date,),
        output_root=config.output_root,
    )
    blockers: list[str] = []
    snapshot_count = int(snapshot_build.get("snapshot_count") or 0)
    if snapshot_count == 0:
        blockers.extend(_rejection_reasons(snapshot_build))
        if not blockers:
            blockers.append("no cutoff-safe snapshots were produced")
    signal_count = int(scan.get("signal_count") or 0)
    status = "blocked" if blockers else ("passed" if signal_count else "passed_no_signal")
    return {
        "schema_version": OPERATOR_SCHEMA,
        "status": status,
        "stage": "scan",
        "market_date": market_date,
        "cutoff_et": cutoff_et,
        "system_invoked_at": local_now.isoformat(),
        "input_adapter": type(adapter).__name__,
        "bars_csv": str(inputs.bars_csv.resolve()),
        "bars_sha256": _sha256_file(inputs.bars_csv),
        "context_csv": str(inputs.context_csv.resolve()),
        "context_sha256": _sha256_file(inputs.context_csv),
        "snapshot_count": snapshot_count,
        "rejected_snapshot_count": int(snapshot_build.get("rejected_count") or 0),
        "decision_count": int(scan.get("decision_count") or 0),
        "signal_count": signal_count,
        "snapshots_path": str(snapshot_path.resolve()),
        "snapshots_sha256": _sha256_file(snapshot_path),
        "rejected_path": snapshot_build.get("rejected_path"),
        "rejected_sha256": _sha256_optional_path(snapshot_build.get("rejected_path")),
        "forward_receipt_path": snapshot_build.get("forward_receipt_path"),
        "forward_receipt_sha256": _sha256_optional_path(
            snapshot_build.get("forward_receipt_path")
        ),
        "decisions_path": scan.get("decisions_path"),
        "decisions_sha256": scan.get("decisions_sha256"),
        "signals_path": scan.get("signals_path"),
        "signals_sha256": scan.get("signals_sha256"),
        "scan_manifest_path": scan.get("run_manifest_path"),
        "scan_manifest_sha256": _sha256_optional_path(scan.get("run_manifest_path")),
        "blockers": blockers,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _run_reconciliation(
    *,
    config: MoverDailyWorkflowConfig,
    adapter: MoverWorkflowInputAdapter,
    market_date: str,
    now: datetime,
) -> dict[str, Any]:
    local_now = now.astimezone(MARKET_TZ)
    market_day = date.fromisoformat(market_date)
    if local_now.date() != market_day:
        raise ValueError("reconciliation market_date must equal the current ET market date")
    session = market_session(market_day)
    if not session.is_trading_day or session.close_time_et is None:
        raise ValueError(
            f"reconciliation market_date is not a published trading session: {market_date}"
        )
    published_close = datetime.combine(
        market_day,
        time.fromisoformat(session.close_time_et),
        tzinfo=MARKET_TZ,
    )
    configured_regular = datetime.combine(
        market_day,
        _clock(config.reconcile_not_before_et),
        tzinfo=MARKET_TZ,
    )
    regular_close = datetime.combine(market_day, _REGULAR_CLOSE, tzinfo=MARKET_TZ)
    post_close_lag = configured_regular - regular_close
    not_before = published_close + post_close_lag
    if local_now < not_before:
        return {
            "schema_version": OPERATOR_SCHEMA,
            "status": "not_applicable_yet",
            "stage": "reconcile",
            "market_date": market_date,
            "cutoff_et": None,
            "system_invoked_at": local_now.isoformat(),
            "published_session": session.to_dict(),
            "published_close_at": published_close.isoformat(),
            "reconcile_not_before_at": not_before.isoformat(),
            "scheduled_cutoff_count": len(config.cutoffs_et),
            "reconciled_cutoff_count": 0,
            "signal_count": 0,
            "closed_trade_count": 0,
            "pending_trade_count": 0,
            "not_entered_count": 0,
            "blockers": [],
            "reason": "published official close plus configured lag has not elapsed",
            "pending_return_semantics": "null_not_zero",
            "research_only": True,
            "broker_execution_enabled": False,
        }
    inputs = adapter.reconciliation_inputs(market_date=market_date)
    _required_file(inputs.bars_csv, "after-close reconciliation bars CSV")
    bars_summary = _validate_reconciliation_bars_receipt(
        inputs.bars_csv,
        market_date=market_date,
        system_received_at=local_now,
        published_close_at=published_close,
    )
    reconciliation_receipt = _retain_reconciliation_source_receipt(
        config.output_root,
        market_date=market_date,
        system_received_at=local_now,
        not_before_at=not_before,
        bars_csv=inputs.bars_csv,
        bars_summary=bars_summary,
        session_payload=session.to_dict(),
    )
    blockers: list[str] = []
    scan_states: list[dict[str, Any]] = []
    for cutoff in config.cutoffs_et:
        scan_state_path = _scan_state_path(config.output_root, market_date, cutoff)
        state = _read_mapping(scan_state_path)
        if not state:
            blockers.append(f"missing scheduled scan state for {cutoff} ET")
            continue
        try:
            receipt = _validated_operator_state_receipt(
                state,
                expected_stage="scan",
                expected_market_date=market_date,
                expected_cutoff_et=cutoff,
                expected_config_fingerprint=config.fingerprint,
                expected_output_root=config.output_root,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(
                f"scan {cutoff} ET state failed immutable validation: {_safe_error(exc)}"
            )
            continue
        workflow_state = dict(receipt["workflow_payload"])
        if str(workflow_state.get("workflow_status") or "") not in _SUCCESS_STATUSES:
            reasons = ", ".join(
                str(value) for value in workflow_state.get("blockers") or ()
            )
            blockers.append(f"scan {cutoff} ET was blocked: {reasons or 'unknown reason'}")
            continue
        scan_states.append(workflow_state)

    reconciliations: list[dict[str, Any]] = []
    analyses: list[dict[str, Any]] = []
    for state in scan_states:
        signals_path = Path(str(state.get("signals_path") or ""))
        scan_manifest_path = Path(str(state.get("scan_manifest_path") or ""))
        try:
            _required_file(signals_path, "retained signals ledger")
            _required_file(scan_manifest_path, "paper-scan run manifest")
            reconciliation = reconcile_paper_signals(
                signals_path=signals_path,
                bars_csv=inputs.bars_csv,
                notional_per_trade=config.notional_per_trade,
                slippage_bps=config.slippage_bps,
                fee_bps=config.fee_bps,
                bar_interval_minutes=config.bar_interval_minutes,
                bar_timestamp_semantics="bar_close",
                output_root=config.output_root,
            )
            reconciliations.append(reconciliation)
            analyses.append(
                analyze(
                    scan_manifest_path=scan_manifest_path,
                    reconcile_manifest_path=Path(
                        str(reconciliation.get("run_manifest_path") or "")
                    ),
                    output_root=config.output_root,
                )
            )
        except Exception as exc:
            blockers.append(
                f"cutoff {state.get('cutoff_et') or 'unknown'} reconciliation failed: "
                f"{_safe_error(exc)}"
            )
    verification = verify(output_root=config.output_root)
    if str(verification.get("status") or "") != "passed":
        failed = [
            str(row.get("check") or "unknown")
            for row in verification.get("checks") or ()
            if isinstance(row, Mapping)
            and row.get("applicable") is not False
            and row.get("passed") is False
        ]
        blockers.append(
            "evidence verification failed: "
            + (", ".join(failed[:10]) if failed else "unspecified verification check")
        )
    report = analyses[-1] if analyses else {}
    closed_count = sum(int(row.get("closed_trade_count") or 0) for row in reconciliations)
    pending_count = sum(int(row.get("pending_trade_count") or 0) for row in reconciliations)
    not_entered_count = sum(int(row.get("not_entered_count") or 0) for row in reconciliations)
    signal_count = sum(int(row.get("signal_count") or 0) for row in reconciliations)
    if blockers:
        status = "blocked"
    elif pending_count:
        status = "incomplete_pending"
        blockers.append(
            f"{pending_count} paper outcome(s) remain incomplete; reconciliation is retryable"
        )
    else:
        status = "passed"
    return {
        "schema_version": OPERATOR_SCHEMA,
        "status": status,
        "stage": "reconcile",
        "market_date": market_date,
        "cutoff_et": None,
        "system_invoked_at": local_now.isoformat(),
        "input_adapter": type(adapter).__name__,
        "bars_csv": str(inputs.bars_csv.resolve()),
        "bars_sha256": _sha256_file(inputs.bars_csv),
        "authoritative_system_received_at": local_now.isoformat(),
        "published_session": session.to_dict(),
        "published_close_at": published_close.isoformat(),
        "reconcile_not_before_at": not_before.isoformat(),
        "reconciliation_receipt_ref": reconciliation_receipt[
            "reconciliation_receipt_ref"
        ],
        "reconciliation_receipt_path": reconciliation_receipt[
            "reconciliation_receipt_path"
        ],
        "reconciliation_receipt_content_sha256": reconciliation_receipt[
            "reconciliation_receipt_content_sha256"
        ],
        "reconciliation_receipt_file_sha256": reconciliation_receipt[
            "reconciliation_receipt_file_sha256"
        ],
        "scheduled_cutoff_count": len(config.cutoffs_et),
        "reconciled_cutoff_count": len(reconciliations),
        "signal_count": signal_count,
        "closed_trade_count": closed_count,
        "pending_trade_count": pending_count,
        "not_entered_count": not_entered_count,
        "reconcile_manifest_paths": [row.get("run_manifest_path") for row in reconciliations],
        "reconcile_manifest_sha256s": [
            _sha256_optional_path(row.get("run_manifest_path")) for row in reconciliations
        ],
        "analysis_path": report.get("report_path"),
        "analysis_sha256": _sha256_optional_path(report.get("report_path")),
        "analysis_series_mode": report.get("analysis_series_mode"),
        "included_run_pair_count": report.get("included_run_pair_count"),
        "excluded_incompatible_run_count": report.get(
            "excluded_incompatible_run_count"
        ),
        "calendar_path": report.get("strategy_daily_calendar_path"),
        "calendar_sha256": _sha256_optional_path(
            report.get("strategy_daily_calendar_path")
        ),
        "calendar_html_path": report.get("strategy_daily_calendar_html_path"),
        "calendar_html_sha256": _sha256_optional_path(
            report.get("strategy_daily_calendar_html_path")
        ),
        "forward_performance_metrics_available": bool(
            report.get("forward_performance_metrics_available")
        ),
        "verification_status": verification.get("status"),
        "blockers": blockers,
        "pending_return_semantics": "null_not_zero",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _notification_events(payload: Mapping[str, Any]) -> list[NotificationEvent]:
    stage = str(payload.get("stage") or "operator")
    day = str(payload.get("market_date") or "unknown-date")
    cutoff = str(payload.get("cutoff_et") or "after-close")
    status = str(payload.get("status") or "blocked")
    if status == "not_applicable_yet":
        return []
    if stage == "scan":
        signal_count = int(payload.get("signal_count") or 0)
        if status == "passed" and signal_count:
            rows = _read_jsonl(Path(str(payload.get("signals_path") or "")))
            if len(rows) != signal_count:
                raise ValueError(
                    "notification signal cohort does not match retained signal_count"
                )
            return [
                _membership_event(
                    title="Dawnstrike Mover Paper Signal",
                    body=(
                        "PAPER ONLY — No order was placed.\n"
                        f"{day} {cutoff} ET: source-validated forward paper signal "
                        f"{index}/{signal_count}.\n"
                        f"{row.get('symbol')} | {row.get('strategy_id')}@"
                        f"{row.get('strategy_version')} | ref "
                        f"{_price(row.get('entry_reference'))} | stop "
                        f"{_price(row.get('stop'))} | target {_price(row.get('target'))}\n"
                        "Simulated next-eligible-bar entry only."
                    ),
                    stage=stage,
                    day=day,
                    cutoff=cutoff,
                    status=status,
                    membership={
                        "kind": "paper_signal",
                        "membership_id": str(row.get("signal_id") or ""),
                        "signal_id": str(row.get("signal_id") or ""),
                        "symbol": str(row.get("symbol") or ""),
                        "strategy_id": str(row.get("strategy_id") or ""),
                        "strategy_version": str(row.get("strategy_version") or ""),
                        "measured_cohort_member": True,
                    },
                    evidence_path=str(payload.get("signals_path") or ""),
                )
                for index, row in enumerate(rows, start=1)
            ]
        elif status == "passed_no_signal":
            body = (
                f"{day} {cutoff} ET: no paper signal cleared the frozen strategy gates. "
                f"{payload.get('snapshot_count', 0)} cutoff-safe snapshot(s) were evaluated. "
                "This is a valid no-trade observation; return is not 0%."
            )
        else:
            body = _blocked_message(payload, prefix=f"{day} {cutoff} ET scan blocked")
        return [
            _membership_event(
                title="Dawnstrike Mover Paper Signals",
                body="PAPER ONLY — No order was placed.\n" + body,
                stage=stage,
                day=day,
                cutoff=cutoff,
                status=status,
                membership={
                    "kind": "no_signal" if status == "passed_no_signal" else "operator_status",
                    "membership_id": f"{day}:{cutoff}:{status}",
                    "measured_cohort_member": False,
                },
            )
        ]

    trades = _reconciled_trades(payload)
    if status == "blocked":
        return [
            _membership_event(
                title="Dawnstrike Mover Reconciliation Status",
                body="PAPER ONLY — No order was placed.\n"
                + _blocked_message(
                    payload,
                    prefix=f"{day} after-close reconciliation blocked",
                ),
                stage=stage,
                day=day,
                cutoff=cutoff,
                status=status,
                membership={
                    "kind": "operator_status",
                    "membership_id": f"{day}:reconcile:{status}",
                    "measured_cohort_member": False,
                },
            )
        ]
    if trades:
        return [
            _membership_event(
                title="Dawnstrike Mover Paper Result",
                body=_trade_result_message(
                    day=day,
                    trade=row,
                    calendar_path=str(payload.get("calendar_html_path") or "unavailable"),
                ),
                stage=stage,
                day=day,
                cutoff=cutoff,
                status=status,
                membership={
                    "kind": "paper_outcome",
                    "membership_id": str(
                        row.get("signal_id") or row.get("observation_id") or ""
                    ),
                    "observation_id": str(row.get("observation_id") or ""),
                    "signal_id": str(row.get("signal_id") or ""),
                    "symbol": str(row.get("symbol") or ""),
                    "strategy_id": str(row.get("strategy_id") or ""),
                    "strategy_version": str(row.get("strategy_version") or ""),
                    "trade_status": str(row.get("status") or ""),
                    "measured_cohort_member": True,
                },
                evidence_path=str(payload.get("analysis_path") or ""),
            )
            for row in trades
        ]

    if status in _SUCCESS_STATUSES or status == "incomplete_pending":
        body = _result_message(payload)
    else:
        body = _blocked_message(payload, prefix=f"{day} after-close reconciliation blocked")
    return [
        _membership_event(
            title="Dawnstrike Mover Paper Results",
            body="PAPER ONLY — No order was placed.\n" + body,
            stage=stage,
            day=day,
            cutoff=cutoff,
            status=status,
            membership={
                "kind": "no_trade_session" if not trades else "operator_status",
                "membership_id": f"{day}:reconcile:{status}",
                "measured_cohort_member": False,
            },
        )
    ]


def _notification_event(payload: Mapping[str, Any]) -> NotificationEvent:
    """Backward-compatible single-event view for callers/tests."""

    return _notification_events(payload)[0]


def _membership_event(
    *,
    title: str,
    body: str,
    stage: str,
    day: str,
    cutoff: str,
    status: str,
    membership: Mapping[str, Any],
    evidence_path: str = "",
) -> NotificationEvent:
    if not str(membership.get("membership_id") or ""):
        raise ValueError("notification delivery membership requires a stable id")
    if len(body) > _MAX_COMPACT_MESSAGE_CHARS:
        raise ValueError(
            "one notification membership exceeds the compact Telegram contract"
        )
    identity = {
        "stage": stage,
        "market_date": day,
        "cutoff_et": cutoff,
        "status": status,
        "membership": dict(membership),
        "evidence_path": evidence_path,
        "message": body,
    }
    fingerprint = _fingerprint(identity)
    return NotificationEvent(
        event_key=(
            f"mover-lab:{day}:{stage}:{cutoff}:"
            f"{membership['membership_id']}:{fingerprint[:20]}"
        ),
        title=title,
        body=body,
        channel_hint="daily_summary",
        payload={
            "telegram_compact_message": body,
            "run_id": f"mover-lab:{day}:{stage}:{cutoff}",
            "market_date": day,
            "research_only": True,
            "broker_execution_enabled": False,
            "evidence_fingerprint": fingerprint,
            "delivery_membership": dict(membership),
            "evidence_path": evidence_path,
        },
    )


def _result_message(payload: Mapping[str, Any]) -> str:
    day = str(payload.get("market_date") or "unknown-date")
    manifest_paths = [Path(str(value)) for value in payload.get("reconcile_manifest_paths") or ()]
    trades: list[dict[str, Any]] = []
    for manifest_path in manifest_paths:
        manifest = _read_mapping(manifest_path)
        trades.extend(_read_jsonl(Path(str(manifest.get("trades_path") or ""))))
    closed = [row for row in trades if row.get("status") == "closed"]
    if closed:
        lines = [
            f"{row.get('symbol')} | {row.get('strategy_id')} | after-cost "
            f"{_pct(row.get('net_return_pct'))} | "
            f"{row.get('reason') or row.get('exit_reason') or 'closed'}"
            for row in closed
        ]
        return (
            f"{day}: {len(closed)} forward paper trade(s) closed.\n"
            + "\n".join(lines)
            + f"\nCalendar: {payload.get('calendar_html_path') or 'unavailable'}"
            + "\nResearch/paper evidence only."
        )
    pending = int(payload.get("pending_trade_count") or 0)
    signal_count = int(payload.get("signal_count") or 0)
    if pending:
        return (
            f"{day}: {pending} paper outcome(s) remain incomplete. Returns stay unavailable, "
            "not 0%."
        )
    if signal_count == 0:
        return (
            f"{day}: no validated mover paper signals existed to reconcile. "
            "No return is reported because no trade occurred."
        )
    return (
        f"{day}: {signal_count} paper signal(s) were evaluated but none produced a closed "
        "trade. Returns remain unavailable, not 0%."
    )


def _trade_result_message(*, day: str, trade: Mapping[str, Any], calendar_path: str) -> str:
    status = str(trade.get("status") or "unknown")
    symbol = str(trade.get("symbol") or "UNKNOWN")
    strategy = str(trade.get("strategy_id") or "unknown_strategy")
    if status == "closed":
        detail = (
            f"after-cost {_pct(trade.get('net_return_pct'))} | "
            f"{trade.get('reason') or trade.get('exit_reason') or 'closed'}"
        )
    elif status == "not_entered":
        detail = "not entered; no return exists"
    else:
        detail = "outcome incomplete; return remains unavailable, not 0%"
    return (
        "PAPER ONLY — No order was placed.\n"
        f"{day}: {symbol} | {strategy} | {detail}.\n"
        f"Calendar: {calendar_path}\nResearch/paper evidence only."
    )


def _reconciled_trades(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for value in payload.get("reconcile_manifest_paths") or ():
        manifest = _read_mapping(Path(str(value)))
        trades.extend(_read_jsonl(Path(str(manifest.get("trades_path") or ""))))
    return trades


def _blocked_message(payload: Mapping[str, Any], *, prefix: str) -> str:
    blockers = [str(value) for value in payload.get("blockers") or ()]
    return (
        prefix
        + ": "
        + ("; ".join(blockers[:8]) if blockers else "required evidence is unavailable")
        + ". No paper return was inferred."
    )


def _deliver_events(
    events: Sequence[NotificationEvent],
    *,
    config: MoverDailyWorkflowConfig,
    channel: str,
    notifiers: Sequence[BaseNotifier] | None,
) -> dict[str, Any]:
    deliveries = [
        _deliver_event(
            event,
            config=config,
            channel=channel,
            notifiers=notifiers,
        )
        for event in events
    ]
    counts = Counter(str(row.get("status") or "unknown") for row in deliveries)
    if counts["delivery_unknown"]:
        status = "delivery_unknown"
    elif counts["delivery_failed"]:
        status = "delivery_failed"
    elif deliveries and counts["duplicate_suppressed"] == len(deliveries):
        status = "duplicate_suppressed"
    elif deliveries:
        status = "delivered"
    else:
        status = "not_applicable"
    payload = {
        "schema_version": f"{OPERATOR_SCHEMA}.notification_bundle",
        "status": status,
        "event_count": len(events),
        "delivered_count": counts["delivered"],
        "duplicate_suppressed_count": counts["duplicate_suppressed"],
        "delivery_unknown_count": counts["delivery_unknown"],
        "delivery_failed_count": counts["delivery_failed"],
        "memberships": deliveries,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if len(deliveries) == 1:
        payload["outbox_path"] = deliveries[0].get("outbox_path")
        if deliveries[0].get("error"):
            payload["error"] = deliveries[0]["error"]
    return payload


def _deliver_event(
    event: NotificationEvent,
    *,
    config: MoverDailyWorkflowConfig,
    channel: str,
    notifiers: Sequence[BaseNotifier] | None,
) -> dict[str, Any]:
    fingerprint = str((event.payload or {}).get("evidence_fingerprint") or "unknown")
    outbox_path = (
        config.output_root
        / "operator"
        / "notifications"
        / "outbox"
        / f"{fingerprint}_{channel}.json"
    )
    existing = _read_mapping(outbox_path)
    if existing:
        if existing.get("event") != _event_payload(event):
            return {
                "status": "delivery_failed",
                "error": "notification outbox event identity mismatch",
                "outbox_path": str(outbox_path.resolve()),
                "delivery_membership": dict(
                    (event.payload or {}).get("delivery_membership") or {}
                ),
            }
        prior_status = str(existing.get("status") or "")
        if prior_status == "attempting":
            existing["status"] = "delivery_unknown"
            existing["error"] = (
                "prior process ended after durable attempt and before acknowledgement"
            )
            existing["automatic_retry_allowed"] = False
            write_json(outbox_path, existing)
            prior_status = "delivery_unknown"
        if prior_status in {"delivered", "delivery_unknown"}:
            return {
                **existing,
                "status": "duplicate_suppressed",
                "original_delivery_status": prior_status,
                "duplicate_suppressed": True,
                "outbox_path": str(outbox_path.resolve()),
            }

    try:
        configured_notifiers = list(notifiers or ())
        if not configured_notifiers:
            scanner_config = load_config(
                env_file=config.env_file,
                database_path=config.notification_db_path,
                notifier_channels=channel,
            )
            configured_notifiers = (
                [ConsoleNotifier()]
                if channel == "console"
                else build_notifiers(scanner_config)
            )
            if channel == "telegram":
                if (
                    scanner_config.telegram_message_style != "compact"
                    or scanner_config.telegram_include_debug_fields
                ):
                    raise ValueError(
                        "operator Telegram proof requires compact style with debug fields disabled"
                    )
                if len(event.body) > scanner_config.telegram_max_summary_chars:
                    raise ValueError(
                        "operator Telegram message exceeds configured summary limit"
                    )
        if len(configured_notifiers) != 1:
            raise ValueError("operator delivery requires exactly one notifier")
        notifier = configured_notifiers[0]
        if notifier.channel != channel:
            raise ValueError("injected notifier channel does not match requested channel")
        store = SQLiteScanStore(config.notification_db_path)
        notification_key = f"{event.event_key}:{channel}"
        if store.has_notification(notification_key):
            duplicate_record = {
                "schema_version": f"{OPERATOR_SCHEMA}.notification_outbox",
                "status": "duplicate_suppressed",
                "original_delivery_status": "delivered",
                "duplicate_suppressed": True,
                "channel": channel,
                "event": _event_payload(event),
                "delivery_membership": dict(
                    (event.payload or {}).get("delivery_membership") or {}
                ),
                "automatic_retry_allowed": False,
                "research_only": True,
                "broker_execution_enabled": False,
            }
            write_json(outbox_path, duplicate_record)
            return {
                **duplicate_record,
                "outbox_path": str(outbox_path.resolve()),
            }
    except Exception as exc:
        failure_record = {
            "schema_version": f"{OPERATOR_SCHEMA}.notification_outbox",
            "status": "delivery_failed",
            "channel": channel,
            "event": _event_payload(event),
            "delivery_membership": dict(
                (event.payload or {}).get("delivery_membership") or {}
            ),
            "automatic_retry_allowed": True,
            "error": _safe_error(exc),
            "research_only": True,
            "broker_execution_enabled": False,
        }
        write_json(outbox_path, failure_record)
        return {**failure_record, "outbox_path": str(outbox_path.resolve())}

    record: dict[str, Any] = {
        "schema_version": f"{OPERATOR_SCHEMA}.notification_outbox",
        "status": "attempting",
        "channel": channel,
        "event": _event_payload(event),
        "delivery_membership": dict(
            (event.payload or {}).get("delivery_membership") or {}
        ),
        "attempt_recorded_at": datetime.now(timezone.utc).isoformat(),
        "automatic_retry_allowed": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    write_json(outbox_path, record)
    try:
        transport_receipt = _validated_transport_receipt(
            configured_notifiers[0].send(event),
            event=event,
            channel=channel,
        )
        inserted = store.record_notification(
            event_key=notification_key,
            channel=channel,
            run_id=str((event.payload or {}).get("run_id") or "") or None,
            ticker=event.ticker,
            payload={
                "title": event.title,
                "body": event.body,
                "channel_hint": event.channel_hint,
                "payload": event.payload or {},
                "transport_receipt": transport_receipt,
            },
        )
        if not inserted:
            raise RuntimeError(
                "provider send completed but durable delivery receipt was not inserted"
            )
        record.update(
            {
                "status": "delivered",
                "acknowledged_at": datetime.now(timezone.utc).isoformat(),
                "transport_receipt": transport_receipt,
                "message_id": transport_receipt.get("message_id"),
            }
        )
        write_json(outbox_path, record)
        return {**record, "outbox_path": str(outbox_path.resolve())}
    except Exception as exc:
        error = _safe_error(exc)
        record.update(
            {
                "status": "delivery_unknown",
                "error": error,
                "automatic_retry_allowed": False,
            }
        )
        write_json(outbox_path, record)
        return {**record, "outbox_path": str(outbox_path.resolve())}


def _validated_transport_receipt(
    value: Mapping[str, Any] | None,
    *,
    event: NotificationEvent,
    channel: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("notifier returned no verifiable transport receipt")
    receipt = dict(value)
    transmitted = str(receipt.get("transmitted_text") or "")
    if not transmitted:
        raise ValueError("transport receipt omitted exact transmitted_text")
    encoded = transmitted.encode("utf-8")
    if int(receipt.get("transmitted_byte_count") or -1) != len(encoded):
        raise ValueError("transport receipt byte count mismatch")
    if str(receipt.get("transmitted_bytes_sha256") or "") != hashlib.sha256(
        encoded
    ).hexdigest():
        raise ValueError("transport receipt byte hash mismatch")
    if channel == "telegram":
        expected = str((event.payload or {}).get("telegram_compact_message") or "")
        if transmitted != expected:
            raise ValueError("Telegram transmitted text differs from exact event membership")
        if receipt.get("message_id") in {None, ""}:
            raise ValueError("Telegram acknowledgement omitted message_id")
    return receipt


def _redeliver_prior(
    prior: dict[str, Any],
    *,
    receipt_payload: Mapping[str, Any],
    config: MoverDailyWorkflowConfig,
    state_path: Path,
    channel: str,
    notifiers: Sequence[BaseNotifier] | None,
) -> dict[str, Any]:
    workflow_payload = receipt_payload.get("workflow_payload")
    if not isinstance(workflow_payload, Mapping):
        raise ValueError("operator receipt workflow_payload must be an object")
    events = _notification_events(workflow_payload)
    prior["notification_events"] = [_event_payload(event) for event in events]
    if events:
        prior["notification_event"] = _event_payload(events[0])
    prior["notification"] = _deliver_events(
        events, config=config, channel=channel, notifiers=notifiers
    )
    prior["notification_status"] = prior["notification"]["status"]
    prior["status"] = str(prior.get("workflow_status") or prior.get("status"))
    write_json(state_path, prior)
    return prior


def _blocked_without_run(
    *,
    config: MoverDailyWorkflowConfig,
    stage: str,
    market_date: str,
    cutoff_et: str | None,
    state_path: Path,
    channel: str,
    notifiers: Sequence[BaseNotifier] | None,
    reason: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": OPERATOR_SCHEMA,
        "status": "blocked",
        "stage": stage,
        "market_date": market_date,
        "cutoff_et": cutoff_et,
        "config_fingerprint": config.fingerprint,
        "operator_state_path": str(state_path.resolve()),
        "workflow_status": "blocked",
        "blockers": [reason],
        "research_only": True,
        "broker_execution_enabled": False,
    }
    events = _notification_events(payload)
    payload["notification_events"] = [_event_payload(event) for event in events]
    if events:
        payload["notification_event"] = _event_payload(events[0])
    payload["notification"] = _deliver_events(
        events, config=config, channel=channel, notifiers=notifiers
    )
    payload["notification_status"] = payload["notification"]["status"]
    return payload


def _event_payload(event: NotificationEvent) -> dict[str, Any]:
    return {
        "event_key": event.event_key,
        "title": event.title,
        "body": event.body,
        "channel_hint": event.channel_hint,
        "payload": event.payload or {},
    }


def _retain_operator_receipt(
    output_root: Path,
    workflow_payload: Mapping[str, Any],
) -> dict[str, Any]:
    artifacts = _operator_artifact_records(workflow_payload)
    receipt_payload = {
        "schema_version": OPERATOR_RECEIPT_SCHEMA,
        "operator_schema_version": OPERATOR_SCHEMA,
        "stage": workflow_payload.get("stage"),
        "market_date": workflow_payload.get("market_date"),
        "cutoff_et": workflow_payload.get("cutoff_et"),
        "config_fingerprint": workflow_payload.get("config_fingerprint"),
        "workflow_status": workflow_payload.get("workflow_status"),
        "workflow_payload": dict(workflow_payload),
        "artifacts": artifacts,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    retained = _write_content_addressed_json(
        output_root / "operator" / "receipts" / "sha256",
        receipt_payload,
    )
    return {
        "operator_receipt_ref": retained["content_ref"],
        "operator_receipt_path": retained["path"],
        "operator_receipt_content_sha256": retained["content_sha256"],
        "operator_receipt_file_sha256": retained["file_sha256"],
    }


def _validated_operator_state_receipt(
    state: Mapping[str, Any],
    *,
    expected_stage: str,
    expected_market_date: str,
    expected_cutoff_et: str | None,
    expected_config_fingerprint: str,
    expected_output_root: Path,
) -> dict[str, Any]:
    expected = {
        "schema_version": OPERATOR_SCHEMA,
        "stage": expected_stage,
        "market_date": expected_market_date,
        "cutoff_et": expected_cutoff_et,
        "config_fingerprint": expected_config_fingerprint,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    for field, value in expected.items():
        if state.get(field) != value:
            raise ValueError(f"operator state {field} mismatch")
    receipt_path = Path(str(state.get("operator_receipt_path") or ""))
    _required_file(receipt_path, "immutable operator receipt")
    file_sha = _sha256_file(receipt_path)
    if file_sha != str(state.get("operator_receipt_file_sha256") or ""):
        raise ValueError("immutable operator receipt file hash mismatch")
    receipt = _read_mapping(receipt_path)
    if not receipt:
        raise ValueError("immutable operator receipt is not a JSON object")
    content_sha = _fingerprint(receipt)
    canonical_receipt_path = (
        expected_output_root
        / "operator"
        / "receipts"
        / "sha256"
        / f"{content_sha}.json"
    ).resolve()
    if receipt_path.resolve() != canonical_receipt_path:
        raise ValueError("immutable operator receipt is outside its canonical path")
    expected_ref = f"sha256:{content_sha}:{receipt_path.resolve()}"
    if (
        content_sha != str(state.get("operator_receipt_content_sha256") or "")
        or expected_ref != str(state.get("operator_receipt_ref") or "")
    ):
        raise ValueError("immutable operator receipt content address mismatch")
    receipt_expected = {
        "schema_version": OPERATOR_RECEIPT_SCHEMA,
        "operator_schema_version": OPERATOR_SCHEMA,
        "stage": expected_stage,
        "market_date": expected_market_date,
        "cutoff_et": expected_cutoff_et,
        "config_fingerprint": expected_config_fingerprint,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    for field, value in receipt_expected.items():
        if receipt.get(field) != value:
            raise ValueError(f"immutable operator receipt {field} mismatch")
    workflow = receipt.get("workflow_payload")
    if not isinstance(workflow, Mapping):
        raise ValueError("immutable operator receipt workflow_payload must be an object")
    for field, value in expected.items():
        if workflow.get(field) != value:
            raise ValueError(f"immutable workflow payload {field} mismatch")
    for field, value in workflow.items():
        if state.get(field) != value:
            raise ValueError(
                f"mutable operator state {field} differs from immutable workflow receipt"
            )
    workflow_status = str(workflow.get("workflow_status") or "")
    if workflow_status != str(receipt.get("workflow_status") or ""):
        raise ValueError("immutable workflow status mismatch")
    if workflow_status != str(state.get("workflow_status") or ""):
        raise ValueError("mutable state workflow status differs from immutable receipt")
    expected_artifacts = _operator_artifact_records(workflow)
    if receipt.get("artifacts") != expected_artifacts:
        raise ValueError("immutable operator receipt artifact inventory mismatch")
    return receipt


def _operator_artifact_records(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    pairs: list[tuple[str, object, object]] = []

    def add(label: str, path_field: str, hash_field: str) -> None:
        path_value = payload.get(path_field)
        hash_value = payload.get(hash_field)
        if path_value in {None, ""} and hash_value in {None, ""}:
            return
        if path_value in {None, ""} or hash_value in {None, ""}:
            raise ValueError(f"operator artifact {label} requires path and sha256")
        pairs.append((label, path_value, hash_value))

    add("bars", "bars_csv", "bars_sha256")
    if payload.get("stage") == "scan":
        add("context", "context_csv", "context_sha256")
        add("snapshots", "snapshots_path", "snapshots_sha256")
        add("rejections", "rejected_path", "rejected_sha256")
        add("forward_receipt", "forward_receipt_path", "forward_receipt_sha256")
        add("decisions", "decisions_path", "decisions_sha256")
        add("signals", "signals_path", "signals_sha256")
        add("scan_manifest", "scan_manifest_path", "scan_manifest_sha256")
    elif payload.get("stage") == "reconcile":
        add(
            "reconciliation_source_receipt",
            "reconciliation_receipt_path",
            "reconciliation_receipt_file_sha256",
        )
        manifest_paths = list(payload.get("reconcile_manifest_paths") or ())
        manifest_hashes = list(payload.get("reconcile_manifest_sha256s") or ())
        if len(manifest_paths) != len(manifest_hashes):
            raise ValueError("reconciliation manifest path/hash cardinality mismatch")
        for index, (path_value, hash_value) in enumerate(
            zip(manifest_paths, manifest_hashes, strict=True)
        ):
            if path_value in {None, ""} or hash_value in {None, ""}:
                raise ValueError("reconciliation manifest path/hash cannot be blank")
            pairs.append((f"reconcile_manifest_{index}", path_value, hash_value))
        add("analysis", "analysis_path", "analysis_sha256")
        add("calendar", "calendar_path", "calendar_sha256")
        add("calendar_html", "calendar_html_path", "calendar_html_sha256")

    records: list[dict[str, str]] = []
    for label, path_value, declared_hash in pairs:
        path = Path(str(path_value))
        _required_file(path, f"operator {label} artifact")
        actual_hash = _sha256_file(path)
        if actual_hash != str(declared_hash):
            raise ValueError(f"operator {label} artifact hash mismatch")
        _validate_retained_artifact_lineage(path)
        records.append(
            {
                "label": label,
                "path": str(path.resolve()),
                "sha256": actual_hash,
            }
        )
    return records


def _validate_retained_artifact_lineage(path: Path) -> None:
    if path.suffix.lower() not in {".json", ".jsonl"}:
        return
    if path.suffix.lower() == ".jsonl":
        values: list[Any] = _read_jsonl(path)
    else:
        try:
            values = [json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"retained JSON artifact cannot be parsed: {path}") from exc
    for value in values:
        _validate_embedded_content_refs(value)
        if isinstance(value, Mapping):
            schema = str(value.get("schema_version") or "")
            if schema.endswith(".paper_scan"):
                _validate_declared_path_hash(value, "snapshots_path", "snapshots_sha256")
                _validate_declared_path_hash(value, "decisions_path", "decisions_sha256")
                _validate_declared_path_hash(value, "signals_path", "signals_sha256")
            elif schema.endswith(".reconcile"):
                _validate_declared_path_hash(value, "signals_path", "signals_sha256")
                _validate_declared_path_hash(value, "bars_csv", "bars_csv_sha256")
                _validate_declared_path_hash(value, "trades_path", "trades_sha256")
            elif schema == RECONCILIATION_RECEIPT_SCHEMA:
                _validate_declared_path_hash(value, "bars_csv", "bars_sha256")


def _validate_declared_path_hash(
    payload: Mapping[str, Any],
    path_field: str,
    hash_field: str,
) -> None:
    path = Path(str(payload.get(path_field) or ""))
    declared = str(payload.get(hash_field) or "")
    _required_file(path, f"manifest {path_field}")
    if not declared or _sha256_file(path) != declared:
        raise ValueError(f"manifest {path_field}/{hash_field} mismatch")
    _validate_retained_artifact_lineage(path)


def _validate_embedded_content_refs(value: Any) -> None:
    if isinstance(value, Mapping):
        for nested in value.values():
            _validate_embedded_content_refs(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _validate_embedded_content_refs(nested)
        return
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return
    parts = value.split(":", 2)
    if len(parts) != 3 or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None:
        raise ValueError("embedded content reference is malformed")
    artifact_path = Path(parts[2])
    _required_file(artifact_path, "embedded content-addressed artifact")
    candidates = {_sha256_file(artifact_path)}
    if artifact_path.suffix.lower() == ".json":
        try:
            raw = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("embedded JSON content reference cannot be parsed") from exc
        candidates.add(_fingerprint(raw))
    if parts[1] not in candidates:
        raise ValueError("embedded content-addressed artifact hash mismatch")


def _validate_reconciliation_bars_receipt(
    bars_csv: Path,
    *,
    market_date: str,
    system_received_at: datetime,
    published_close_at: datetime,
) -> dict[str, Any]:
    timestamps: list[datetime] = []
    current_session: list[datetime] = []
    with bars_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if "timestamp" not in (reader.fieldnames or ()):
            raise ValueError("reconciliation bars CSV requires timestamp")
        for row_number, row in enumerate(reader, start=2):
            parsed = datetime.fromisoformat(str(row.get("timestamp") or "").replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(
                    f"reconciliation bar row {row_number} timestamp must be timezone-aware"
                )
            if parsed > system_received_at:
                raise ValueError(
                    f"reconciliation bar row {row_number} is after authoritative system receipt"
                )
            local = parsed.astimezone(MARKET_TZ)
            if local.date().isoformat() == market_date:
                if local > published_close_at:
                    raise ValueError(
                        f"reconciliation bar row {row_number} is after published close"
                    )
                current_session.append(local)
            timestamps.append(parsed)
    if not timestamps:
        raise ValueError("reconciliation bars CSV contains no rows")
    if not current_session or max(current_session) != published_close_at:
        raise ValueError(
            "reconciliation bars must retain the authoritative published close bar"
        )
    return {
        "row_count": len(timestamps),
        "current_session_row_count": len(current_session),
        "latest_observation_at": max(timestamps).isoformat(),
        "published_close_at": published_close_at.isoformat(),
        "no_bar_after_system_receipt": True,
    }


def _retain_reconciliation_source_receipt(
    output_root: Path,
    *,
    market_date: str,
    system_received_at: datetime,
    not_before_at: datetime,
    bars_csv: Path,
    bars_summary: Mapping[str, Any],
    session_payload: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_version": RECONCILIATION_RECEIPT_SCHEMA,
        "market_date": market_date,
        "system_received_at": system_received_at.isoformat(),
        "not_before_at": not_before_at.isoformat(),
        "bars_csv": str(bars_csv.resolve()),
        "bars_sha256": _sha256_file(bars_csv),
        "bars_summary": dict(bars_summary),
        "published_session": dict(session_payload),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    retained = _write_content_addressed_json(
        output_root
        / "operator"
        / "source_receipts"
        / "reconciliation"
        / market_date
        / "sha256",
        payload,
    )
    return {
        "reconciliation_receipt_ref": retained["content_ref"],
        "reconciliation_receipt_path": retained["path"],
        "reconciliation_receipt_content_sha256": retained["content_sha256"],
        "reconciliation_receipt_file_sha256": retained["file_sha256"],
    }


def _write_content_addressed_json(root: Path, payload: Mapping[str, Any]) -> dict[str, str]:
    content_sha = _fingerprint(payload)
    path = root / f"{content_sha}.json"
    if path.is_file():
        retained = _read_mapping(path)
        if retained != dict(payload):
            raise OSError(f"content-addressed artifact collision: {path}")
    else:
        write_json(path, dict(payload))
    if _read_mapping(path) != dict(payload):
        raise OSError(f"content-addressed artifact write verification failed: {path}")
    return {
        "content_ref": f"sha256:{content_sha}:{path.resolve()}",
        "path": str(path.resolve()),
        "content_sha256": content_sha,
        "file_sha256": _sha256_file(path),
    }


def _rejection_reasons(snapshot_build: Mapping[str, Any]) -> list[str]:
    path = Path(str(snapshot_build.get("rejected_path") or ""))
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    counts: Counter[str] = Counter()
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        reason = str(row.get("reason") or "unspecified rejection")
        detail = str(row.get("detail") or "").strip()
        counts[f"{reason}: {detail}" if detail else reason] += 1
    return [f"{reason} ({count})" for reason, count in counts.most_common(8)]


def _scan_state_path(root: Path, market_date: str, cutoff_et: str) -> Path:
    token = cutoff_et.replace(":", "")
    return root / "operator" / "runs" / market_date / f"scan_{token}.json"


def _reconcile_state_path(root: Path, market_date: str) -> Path:
    return root / "operator" / "runs" / market_date / "reconcile.json"


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return dict(raw) if isinstance(raw, Mapping) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, Mapping):
            rows.append(dict(row))
    return rows


def _required_text(raw: Mapping[str, Any], field: str) -> str:
    value = str(raw.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


def _required_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise ValueError(f"{label} is missing: {path}")


def _market_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _clock(value: str) -> time:
    parsed = time.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError("operator clock values must be timezone-free ET clocks")
    return parsed


def _aware_now(value: datetime | None) -> datetime:
    observed = value or datetime.now(tz=MARKET_TZ)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("operator now must include a timezone")
    return observed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_optional_path(value: object) -> str | None:
    if value in {None, ""}:
        return None
    path = Path(str(value))
    _required_file(path, "retained artifact")
    return _sha256_file(path)


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_error(exc: object) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ")
    text = re.sub(
        r"https://api\.telegram\.org/bot[^/\s]+/",
        "https://api.telegram.org/bot<redacted>/",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?i)(bot\d+:)[A-Za-z0-9_-]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)((?:telegram_)?(?:bot_)?token\s*[=:]\s*)\S+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)((?:telegram_)?chat_id\s*[=:]\s*)-?\d+",
        r"\1<redacted>",
        text,
    )
    return text[:1000] or type(exc).__name__


def _price(value: object) -> str:
    try:
        return f"${float(str(value)):,.4f}"
    except (TypeError, ValueError):
        return "unavailable"


def _pct(value: object) -> str:
    try:
        return f"{float(str(value)):+.4f}%"
    except (TypeError, ValueError):
        return "unavailable"


__all__ = [
    "MoverDailyWorkflowConfig",
    "MoverWorkflowInputAdapter",
    "MoverWorkflowInputs",
    "RetainedCsvMoverInputAdapter",
    "run_mover_daily_workflow",
]
