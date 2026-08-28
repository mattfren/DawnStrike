"""AlphaOps v4 orchestration services."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.ai.strategy_gap_resolver import StrategyGapResolver
from intraday_scanner.alpha.alert_gate import apply_alert_gates
from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION, AlphaModel
from intraday_scanner.alpha.feature_factory import build_feature_vector
from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.plan_constructor import (
    COMPLETE,
    NO_VALID_PLAN,
    construct_alphaops_v5_plan,
)
from intraday_scanner.alpha.regime_detector import detect_regime
from intraday_scanner.alpha.risk_governor import evaluate_risk
from intraday_scanner.alpha.run_contracts import AlphaRunContract, build_alpha_run_contract
from intraday_scanner.alpha.v5_policy import DEFAULT_V5_POLICY, alphaops_strategy_contract
from intraday_scanner.alpha.v6.decision_ledger import build_candidate_decisions
from intraday_scanner.config import load_config
from intraday_scanner.dashboard.operator_data_service import calculate_missing_outcome_status
from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.errors import DataProviderError, SnapshotValidationError, StorageError
from intraday_scanner.market_calendar import (
    MarketSessionDecision,
    core_session_phase,
    session_for_timestamp,
)
from intraday_scanner.models import utc_now_iso
from intraday_scanner.notifiers import (
    BaseNotifier,
    ConsoleNotifier,
    NotificationEvent,
    build_notifiers,
    dispatch_events,
)
from intraday_scanner.notifiers.telegram_formatter import (
    format_alpha_monitor,
    format_alpha_no_trade,
    format_alpha_summary,
    format_alpha_watch,
)
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.providers.nasdaq_halt_provider import (
    attach_halt_status,
    collect_trade_halts,
)
from intraday_scanner.providers.sec_edgar_provider import (
    collect_sec_risk,
    enrich_rows_with_sec_risk,
)
from intraday_scanner.providers.web_source_base import get_source, load_web_sources_config
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.alpha_official_cohort_service import (
    build_official_cohort_row,
    membership_sha256,
    validate_or_recover_official_cohort,
)
from intraday_scanner.services.alpha_v6_universe_service import (
    active_alpha_v6_membership_by_ticker,
    register_alpha_v6_universe,
)
from intraday_scanner.services.candidate_news_service import enrich_candidate_news
from intraday_scanner.services.learning_service import (
    load_production_alpha_learning_labels,
    run_alpha_learning,
)
from intraday_scanner.services.luna_core_universe_service import (
    build_core_universe_contract,
    core_discovery_data_eligible,
    discover_core_universe_rows,
    rank_core_universe_rows,
    write_core_universe_contract,
    write_snapshot_rows,
)
from intraday_scanner.services.luna_research_slate_service import (
    AuthenticatedStrategyReceiptResolver,
    apply_publication_semantics,
    build_ranked_research_slate,
    official_publication_rows,
    persist_ranked_research_slate,
    row_research_admissible,
    validate_ranked_research_slate,
    validated_frozen_selection_signal,
)
from intraday_scanner.services.morning_strategy_adapter import (
    GOVERNED_SOURCE_LABEL,
    LEGACY_SOURCE_LABEL,
    adapt_prior_session_paper_ops,
)
from intraday_scanner.services.premarket_enrichment_service import enrich_premarket_rows
from intraday_scanner.services.price_observation_service import collect_price_observations
from intraday_scanner.services.return_attribution_service import (
    _build_no_trade_historical_signal,
    record_alpha_historical_signals,
    record_monitor_signal_events,
    record_no_trade_historical_signal,
)
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.services.signal_review_service import (
    monitor_alpha_signals,
    review_alpha_signals,
)
from intraday_scanner.services.source_reliability_service import build_source_reliability
from intraday_scanner.services.strategy_decision_service import StrategyDecisionService
from intraday_scanner.services.web_collection_service import web_auto_collect, web_source_doctor
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

EASTERN = ZoneInfo("America/New_York")
DEFAULT_DB_PATH = "data/shadow_real.sqlite"
DEFAULT_WEB_CONFIG = "config/web_sources.yaml"
LEGACY_ALPHAOPS_STRATEGY_ID = "alphaops_v4"
# Public compatibility name for pre-V5 evidence/recovery callers.
ALPHAOPS_STRATEGY_ID = LEGACY_ALPHAOPS_STRATEGY_ID
ALPHAOPS_OFFICIAL_COHORT = "official_telegram"
ALPHAOPS_RADAR_COHORT = "research_radar"
ALPHAOPS_RADAR_VERSION = "dawnstrike-research-radar-v1"
_LEGACY_PICK_PATTERN = re.compile(
    r"^\s*\d+\)\s+([A-Z][A-Z0-9.-]{0,11})\s+-\s+Opportunity\b",
    re.MULTILINE,
)
_LEGACY_PICK_COUNT_PATTERN = re.compile(
    r"\|\s*(\d+)\s+(?:picks?|names?)\s*\|",
    re.IGNORECASE,
)


def alpha_morning(
    *,
    config_path: str | Path = DEFAULT_WEB_CONFIG,
    db_path: str | Path = DEFAULT_DB_PATH,
    out_dir: str | Path = "outputs/alpha_morning",
    notify: str = "console",
    dry_run: bool = False,
    as_of: datetime | None = None,
    core_universe_manifest: str | Path | None = None,
    market_date: str | None = None,
    paper_ops_root: str | Path | None = None,
) -> dict[str, Any]:
    return alpha_cycle(
        config_path=config_path,
        db_path=db_path,
        out_dir=out_dir,
        notify=notify,
        dry_run=dry_run,
        cycle_name="alpha_morning",
        as_of=as_of,
        core_universe_manifest=core_universe_manifest,
        market_date=market_date,
        paper_ops_root=paper_ops_root,
    )


def alpha_cycle(
    *,
    config_path: str | Path = DEFAULT_WEB_CONFIG,
    db_path: str | Path = DEFAULT_DB_PATH,
    out_dir: str | Path = "outputs/alpha_cycle",
    notify: str = "console",
    dry_run: bool = False,
    cycle_name: str = "alpha_cycle",
    as_of: datetime | None = None,
    core_universe_manifest: str | Path | None = None,
    market_date: str | None = None,
    paper_ops_root: str | Path | None = None,
) -> dict[str, Any]:
    if market_date:
        parsed_market_date = _parse_market_date(market_date)
        if as_of is not None and as_of.date().isoformat() != parsed_market_date:
            raise ValueError("market_date must match as_of date")
        if as_of is None:
            as_of = datetime.fromisoformat(f"{parsed_market_date}T12:00:00+00:00")
    cycle_observed_at = as_of or datetime.now(timezone.utc)
    # One immutable cycle decision timestamp must cross the collection,
    # enrichment, scoring, and plan-freeze boundaries.  ``score_universe``
    # intentionally records its own wall-clock scan creation time (rounded to
    # seconds), which is not a safe join key for the point-in-time enrichment
    # receipt when callers provide a microsecond ``as_of``.  Normalize once and
    # carry this exact value through every AlphaOps decision contract.
    if cycle_observed_at.tzinfo is None:
        cycle_decision_at = cycle_observed_at.replace(tzinfo=timezone.utc)
    else:
        cycle_decision_at = cycle_observed_at.astimezone(timezone.utc)
    cycle_decision_timestamp = cycle_decision_at.isoformat()
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    core_universe = build_core_universe_contract(
        core_universe_manifest,
        observed_at=cycle_decision_at,
        market_date=market_date or cycle_decision_at.date().isoformat(),
    )
    write_core_universe_contract(core_universe, output_dir / "core_universe_contract.json")
    session_gate = _scheduled_session_gate(notify=notify, as_of=as_of)
    if session_gate is not None:
        phase = core_session_phase(as_of)
        skipped_result: dict[str, Any] | None = None
        if not session_gate.is_trading_day:
            skipped_result = _session_skip_result(
                run_type=cycle_name,
                status="skipped_market_closed",
                decision=session_gate,
            )
        elif phase != "before_core_session":
            skipped_result = _session_skip_result(
                run_type=cycle_name,
                status="skipped_outside_premarket_session",
                decision=session_gate,
                phase=phase,
            )
    else:
        skipped_result = None
    if skipped_result is not None:
        skipped_result["out_dir"] = str(output_dir)
        _write_json(output_dir / "alpha_session_gate.json", skipped_result)
        return skipped_result
    store = SQLiteScanStore(db_path)
    store.initialize()
    collection = web_auto_collect(
        config_path=config_path,
        db_path=db_path,
        out_dir=output_dir / "web_collect",
        persist=True,
        print_rows=False,
        observed_at=cycle_decision_at,
    )
    source_summary = dict(collection.get("source_summary") or {})
    source_summary["require_watcher_proof"] = True
    source_summary["core_universe"] = {
        "contract_status": core_universe.get("status"),
        "contract_membership_count": core_universe.get("membership_count", 0),
        "contract_hash_sha256": core_universe.get("content_hash_sha256"),
        "requested_market_date": core_universe.get("requested_market_date"),
        "index_verdicts": core_universe.get("index_verdicts") or {},
        "raw_artifact_hashes": core_universe.get("raw_artifact_hashes") or [],
        "canonical_member_set_hash_sha256": core_universe.get("canonical_member_set_hash_sha256")
        or "",
    }
    mover_source_failed = collection.get("status") != "success"
    mover_snapshot_count = len(list(collection.get("rows") or [])) if not mover_source_failed else 0
    core_only_recovery = False
    core_discovery_recovery: dict[str, Any] | None = None
    source_reliability = build_source_reliability(
        source_summary,
        outcomes=load_production_alpha_learning_labels(store),
        previous=store.load_alpha_source_reliability(),
    )
    if source_reliability:
        store.persist_alpha_source_reliability(source_reliability)

    if collection.get("status") != "success" and core_universe.get("status") == "READY":
        # A mover-source outage is lane-local.  A READY core manifest may still
        # supply independent read-only snapshots for the Alpha cycle.
        recovery_config = _alphaops_scanner_config(
            load_config(
                provider="csv",
                output_dir=output_dir / "core_recovery_scan",
                database_path=Path(db_path),
            )
        )
        recovery = discover_core_universe_rows(
            core_universe,
            config=recovery_config,
            observed_at=cycle_decision_at,
        )
        recovery_rows = rank_core_universe_rows(recovery.get("rows") or [])
        if core_discovery_data_eligible(recovery) and recovery_rows:
            recovery_path = write_snapshot_rows(
                recovery_rows, output_dir / "web_collect" / "core_recovery_snapshot.csv"
            )
            source_summary["mover_lane_status"] = "SOURCE_FAILED"
            source_summary["mover_lane_reason"] = str(
                source_summary.get("top_failure_reason") or "mover collection failed"
            )
            source_summary["status"] = "success"
            core_only_recovery = True
            core_discovery_recovery = recovery
            source_summary["core_universe"] = {
                **recovery,
                "eligible_count": len(recovery_rows),
                "contract_status": core_universe.get("status"),
                "contract_membership_count": core_universe.get("membership_count", 0),
                "contract_hash_sha256": core_universe.get("content_hash_sha256"),
                "requested_market_date": core_universe.get("requested_market_date"),
                "index_verdicts": core_universe.get("index_verdicts") or {},
                "raw_artifact_hashes": core_universe.get("raw_artifact_hashes") or [],
                "canonical_member_set_hash_sha256": core_universe.get(
                    "canonical_member_set_hash_sha256"
                )
                or "",
            }
            collection = {
                **collection,
                "status": "success",
                "rows": recovery_rows,
                "snapshot_path": str(recovery_path),
            }
    if collection.get("status") != "success":
        review = review_alpha_signals([], source_summary=source_summary)
        no_data_scan_id = f"{cycle_name}:source_failure:{cycle_decision_timestamp[:10]}"
        no_data_generated_at = cycle_decision_timestamp
        no_data_no_trade_row = _build_no_trade_historical_signal(
            scan_id=no_data_scan_id,
            generated_at=no_data_generated_at,
            reason=str(review["decision"]["reason"]),
            source_summary=source_summary,
            candidate_count=int(source_summary.get("candidate_count") or 0),
        )
        luna_research_slate = build_ranked_research_slate(
            [],
            target=5,
            data_eligible=False,
            shortfall_reason=str(review["decision"].get("reason") or "DATA_UNAVAILABLE"),
            generated_at=no_data_generated_at,
            market_date=no_data_generated_at[:10],
            scan_id=no_data_scan_id,
            canonical_member_ids=[
                str(row.get("symbol") or row.get("ticker") or "")
                for row in core_universe.get("members") or []
            ],
            require_safety=True,
        )
        slate_path = output_dir / "ranked_research_slate.json"
        persist_ranked_research_slate(luna_research_slate, slate_path)
        luna_research_slate = _load_frozen_luna_slate(
            slate_path, market_date=no_data_generated_at[:10]
        )
        # A source outage can be a retry of a successful same-day cycle.  The
        # first-writer slate, rather than the failed attempt's empty input, is
        # authoritative in that case.  Rehydrate its publication semantics
        # against the persisted receipt store before constructing any retry
        # event; a missing receipt must never be silently demoted to Tier 1.
        receipt_verifier = _persisted_strategy_receipt_verifier(
            store,
            market_date=no_data_generated_at[:10],
        )
        frozen_slate_rows = list(luna_research_slate.get("rows") or [])
        frozen_slate_nonempty = bool(frozen_slate_rows)
        slate_publication_rows: list[dict[str, Any]] = []
        if frozen_slate_nonempty:
            try:
                slate_publication_rows = apply_publication_semantics(
                    frozen_slate_rows,
                    slate=luna_research_slate,
                    coverage={"lanes": luna_research_slate.get("lane_statuses") or {}},
                    require_watcher_proof=True,
                    receipt_verifier=receipt_verifier,
                )
            except (TypeError, ValueError, SnapshotValidationError) as exc:
                raise SnapshotValidationError(
                    "FROZEN_SLATE_PUBLICATION_EVIDENCE_MISSING: persisted frozen "
                    "slate could not be authenticated for source-failure retry"
                ) from exc
            if len(slate_publication_rows) != int(
                luna_research_slate.get("published_count") or 0
            ):
                raise SnapshotValidationError(
                    "FROZEN_SLATE_PUBLICATION_EVIDENCE_MISSING: persisted frozen "
                    "slate publication rows could not be rehydrated"
                )
            strategy_id, strategy_version = alphaops_strategy_contract(no_data_generated_at)
            existing_cohort = store.load_official_strategy_cohort(
                market_date=no_data_generated_at[:10],
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                cohort=ALPHAOPS_OFFICIAL_COHORT,
            )
            if existing_cohort is not None:
                existing_selections = store.load_signal_selections(
                    scan_id=str(existing_cohort.get("scan_id") or ""),
                    event_key=str(existing_cohort.get("event_key") or ""),
                    strategy_id=strategy_id,
                    cohort=ALPHAOPS_OFFICIAL_COHORT,
                    limit=100,
                )
                if (
                    not existing_selections
                    or membership_sha256(existing_selections)
                    != str(existing_cohort.get("membership_sha256") or "")
                ):
                    raise SnapshotValidationError(
                        "FROZEN_SLATE_PUBLICATION_EVIDENCE_MISSING: persisted official "
                        "cohort membership is incomplete"
                    )
                expected_official = [
                    row
                    for row in existing_selections
                    if str(row.get("decision") or "").lower() != "no_trade"
                    and str(row.get("ticker") or "").upper() != "NO_TRADE"
                ]
                publication_by_selection_id = {
                    str(row.get("research_selection_id") or ""): row
                    for row in slate_publication_rows
                }
                if expected_official:
                    for selection in expected_official:
                        publication_row = publication_by_selection_id.get(
                            str(
                                (selection.get("payload_json") or {})
                                .get("publication_row", {})
                                .get("research_selection_id")
                                or ""
                            )
                        )
                        if (
                            publication_row is None
                            or publication_row.get("publication_tier")
                            not in {"PAPER_PLAN_QUALIFIED", "ALERTABLE_PAPER_ENTRY"}
                            or not receipt_verifier.verify(publication_row)
                        ):
                            raise SnapshotValidationError(
                                "FROZEN_SLATE_PUBLICATION_EVIDENCE_MISSING: persisted "
                                "official Tier 2/3 row lacks exact authenticated receipt"
                            )
        source_summary["ranked_research_slate"] = luna_research_slate
        source_summary["ranked_research_publication_rows"] = slate_publication_rows
        source_summary["ranked_research_slate_lineage"] = {
            "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
            "slate_id": str(luna_research_slate.get("slate_id") or ""),
            "slate_content_hash_sha256": str(
                luna_research_slate.get("content_hash_sha256") or ""
            ),
            "frozen_source_scan_id": str(luna_research_slate.get("scan_id") or ""),
            "current_scan_id": no_data_scan_id,
            "reuse_status": (
                "CURRENT_SCAN"
                if str(luna_research_slate.get("scan_id") or "") == no_data_scan_id
                else "GOVERNED_DAILY_FREEZE_REUSE"
            ),
        }
        selected_signals = official_publication_rows(slate_publication_rows, limit=3)
        official_no_trade = not selected_signals
        publication_review = dict(review)
        publication_decision = dict(review["decision"])
        if not official_no_trade:
            publication_decision.update(
                {
                    "no_trade": False,
                    "decision_tier": "clean_edge",
                    "reason": "Immutable frozen Tier 2/3 paper-plan cohort selected.",
                    "primary_reason_code": "frozen_slate_official_selection",
                }
            )
            publication_review["decision"] = publication_decision
            publication_review["watchlist"] = selected_signals
        frozen_manifest = _load_frozen_official_notification_manifest(
            store, selected_at=no_data_generated_at
        )
        message = (
            str(frozen_manifest.get("body") or "")
            if frozen_manifest is not None
            else (
                format_alpha_watch(
                    signals=slate_publication_rows,
                    edge_label=_edge_label(selected_signals),
                    source_summary=source_summary,
                    blocked_signals=list(review.get("blocked") or []),
                    generated_at=no_data_generated_at,
                    target_count=int(luna_research_slate.get("target_count") or 0),
                    published_count=int(luna_research_slate.get("published_count") or 0),
                    slate_shortfall_reason=str(
                        luna_research_slate.get("slate_shortfall_reason") or ""
                    ),
                )
                if not official_no_trade
                else format_alpha_no_trade(
                    reason=str(review["decision"]["reason"]),
                    next_action=str(review["decision"]["next_action"]),
                    research_signals=slate_publication_rows,
                    target_count=int(luna_research_slate.get("target_count") or 0),
                    published_count=int(luna_research_slate.get("published_count") or 0),
                    slate_shortfall_reason=str(
                        luna_research_slate.get("slate_shortfall_reason") or ""
                    ),
                )
            )
        )
        events = [
            _official_selection_notification_event(
                no_data_scan_id,
                "alpha_no_trade" if official_no_trade else "alpha_morning_watch",
                "Dawnstrike Alpha Check" if official_no_trade else "Dawnstrike Alpha Watch",
                message,
                selected_signals=selected_signals,
                research_signals=slate_publication_rows,
            )
        ]
        no_data_no_trade_row = (
            _build_no_trade_historical_signal(
                scan_id=no_data_scan_id,
                generated_at=no_data_generated_at,
                reason=str(publication_decision["reason"]),
                source_summary=source_summary,
                candidate_count=int(source_summary.get("candidate_count") or 0),
            )
            if official_no_trade
            else None
        )
        frozen_cohort_retry = _govern_frozen_official_cohort_retry(
            store,
            scan_id=no_data_scan_id,
            selected_signals=selected_signals
            or ([no_data_no_trade_row] if no_data_no_trade_row is not None else []),
            decision=publication_decision,
            selected_at=no_data_generated_at,
            event=events[0],
        )
        if frozen_cohort_retry is None:
            selected_rows, selection_stats = _persist_official_selections(
                store,
                scan_id=no_data_scan_id,
                selected_signals=selected_signals
                or ([no_data_no_trade_row] if no_data_no_trade_row is not None else []),
                decision=publication_decision,
                selected_at=no_data_generated_at,
                event=events[0],
                slate=luna_research_slate if frozen_slate_nonempty else None,
            )
            if official_no_trade:
                record_no_trade_historical_signal(
                    store,
                    scan_id=no_data_scan_id,
                    generated_at=no_data_generated_at,
                    reason=str(publication_decision["reason"]),
                    source_summary=source_summary,
                    candidate_count=int(source_summary.get("candidate_count") or 0),
                )
            selection_scan_id = no_data_scan_id
            selection_selected_at = no_data_generated_at
        else:
            events = [frozen_cohort_retry["event"]]
            selected_rows = list(frozen_cohort_retry["selections"])
            selection_stats = dict(frozen_cohort_retry["stats"])
            selection_scan_id = str(frozen_cohort_retry["scan_id"])
            selection_selected_at = str(frozen_cohort_retry["selected_at"])
        radar_selection_rows, radar_selection_stats = _persist_research_radar_selections(
            store,
            scan_id=selection_scan_id,
            radar=frozen_slate_rows if frozen_slate_nonempty else [],
            slate=luna_research_slate,
            selected_at=selection_selected_at,
            event=events[0],
        )
        official_signal_ids = {
            str(row.get("signal_id") or "") for row in selected_rows
        }
        delivery_selection_rows = [
            *selected_rows,
            *[
                row
                for row in radar_selection_rows
                if str(row.get("signal_id") or "") not in official_signal_ids
            ],
        ]
        preexisting_notification_keys = _existing_notification_keys(
            store,
            events=events,
            notify=notify,
        )
        source_contract_args: dict[str, Any] = {
            "scan_id": no_data_scan_id,
            "generated_at": no_data_generated_at,
            "ranked_count": len(slate_publication_rows),
            "signals": slate_publication_rows,
            "review": publication_review,
            "source_summary": source_summary,
            "enrichment_summary": None,
            "receipt_verifier": receipt_verifier,
        }
        canonical_contract_path = output_dir / "alpha_run_contract.json"
        prior_canonical_contract = (
            canonical_contract_path.read_bytes() if canonical_contract_path.exists() else None
        )
        retry_contract_artifact = (
            "alpha_run_contract_retry_attempt.json"
            if prior_canonical_contract is not None
            else "alpha_run_contract.json"
        )
        cycle_artifact_path = output_dir / "alpha_cycle.json"
        prior_cycle_artifact = (
            cycle_artifact_path.read_bytes() if cycle_artifact_path.exists() else None
        )
        retry_cycle_artifact = (
            "alpha_cycle_retry_attempt.json"
            if prior_cycle_artifact is not None
            else "alpha_cycle.json"
        )
        _persist_run_contract(
            output_dir,
            **source_contract_args,
            notification_stats={},
            notification_channel=notify,
            notification_dry_run=dry_run,
            notification_status_override="pending",
            artifact_name=retry_contract_artifact,
        )
        try:
            notification_stats = _dispatch(
                events,
                notify=notify,
                db_path=db_path,
                dry_run=dry_run,
            )
        except Exception:
            _persist_notification_delivery_memberships(
                store,
                selections=delivery_selection_rows,
                events=events,
                notify=notify,
                preexisting_notification_keys=preexisting_notification_keys,
            )
            _persist_run_contract(
                output_dir,
                **source_contract_args,
                notification_stats={},
                notification_channel=notify,
                notification_dry_run=dry_run,
                notification_status_override="delivery_failed",
                artifact_name=retry_contract_artifact,
            )
            raise
        notification_deliveries = _persist_notification_delivery_memberships(
            store,
            selections=delivery_selection_rows,
            events=events,
            notify=notify,
            preexisting_notification_keys=preexisting_notification_keys,
        )
        run_contract = _persist_run_contract(
            output_dir,
            **source_contract_args,
            notification_stats=notification_stats,
            notification_channel=notify,
            notification_dry_run=dry_run,
            artifact_name=retry_contract_artifact,
        )
        _link_notification_events(
            store,
            scan_id=selection_scan_id,
            events=events,
            notify=notify,
            dry_run=dry_run,
            signal_ids=[str(row["signal_id"]) for row in selected_rows],
            notification_deliveries=notification_deliveries,
        )
        no_data_result: dict[str, Any] = {
            "status": "source_failed_retry" if frozen_slate_nonempty else "no_trade",
            "run_type": cycle_name,
            "scan_id": no_data_scan_id,
            "source_summary": source_summary,
            "review": publication_review,
            "selection_stats": selection_stats,
            "research_radar": radar_selection_rows,
            "research_radar_selection_stats": radar_selection_stats,
            "notification_stats": notification_stats,
            "notification_deliveries": notification_deliveries,
            "run_contract": run_contract.to_dict(),
            "out_dir": str(output_dir),
            "core_universe": core_universe,
            "ranked_research_slate": luna_research_slate,
        }
        if session_gate is not None:
            no_data_result["session_gate"] = session_gate.to_dict()
        _write_json(output_dir / retry_cycle_artifact, no_data_result)
        return no_data_result

    scanner_config = load_config(
        provider="csv",
        output_dir=output_dir / "scan",
        database_path=Path(db_path),
    )
    source_config = load_web_sources_config(config_path)
    fixture_mode = any(
        source.enabled and bool(source.fixture_path) for source in source_config.sources
    )
    if not fixture_mode:
        scanner_config = _alphaops_scanner_config(scanner_config)
    if core_only_recovery:
        scanner_config = scanner_config.with_overrides(min_gap_pct=0.0, ideal_gap_low_pct=1.0)
    core_discovery = (
        core_discovery_recovery
        if core_discovery_recovery is not None
        else discover_core_universe_rows(
            core_universe,
            config=scanner_config,
            observed_at=cycle_decision_at,
        )
        if not fixture_mode
        else {
            "status": "BLOCKED_EXTERNAL",
            "coverage_status": "DATA_UNAVAILABLE",
            "rows": [],
            "reason": "fixture mode has no current core snapshot provider",
            "requested_count": int(core_universe.get("membership_count") or 0),
            "returned_count": 0,
            "eligible_count": 0,
            "fresh_count": 0,
            "stale_count": 0,
            "missing_count": int(core_universe.get("membership_count") or 0),
            "unknown_count": 0,
            "duplicate_count": 0,
            "coverage_receipts": [],
            "coverage_receipt_ids": [],
            "coverage_receipt_hashes": [],
        }
    )
    source_summary["core_universe"] = {
        **core_discovery,
        "contract_status": core_universe.get("status"),
        "contract_membership_count": core_universe.get("membership_count", 0),
        "contract_hash_sha256": core_universe.get("content_hash_sha256"),
        "requested_market_date": core_universe.get("requested_market_date"),
        "index_verdicts": core_universe.get("index_verdicts") or {},
        "raw_artifact_hashes": core_universe.get("raw_artifact_hashes") or [],
        "canonical_member_set_hash_sha256": core_universe.get("canonical_member_set_hash_sha256")
        or "",
    }
    core_eligible_rows = (
        rank_core_universe_rows(core_discovery.get("rows") or [])
        if core_discovery_data_eligible(core_discovery)
        else []
    )
    if core_eligible_rows and not fixture_mode:
        halt_source = get_source(source_config, "nasdaq_halts")
        if halt_source is not None and halt_source.enabled:
            halt_summary = collect_trade_halts(
                source=halt_source,
                config=source_config,
                out_dir=output_dir / "core_halts",
                store=store,
                persist=True,
            )
            core_eligible_rows = attach_halt_status(
                core_eligible_rows,
                list(halt_summary.get("events") or []),
                feed_verified=str(halt_summary.get("status") or "") == "success",
            )
            core_discovery["halt_evidence"] = {
                "status": halt_summary.get("status"),
                "event_count": len(halt_summary.get("events") or []),
            }
        else:
            core_eligible_rows = attach_halt_status(core_eligible_rows, [], feed_verified=False)
            core_discovery["halt_evidence"] = {
                "status": "UNAVAILABLE",
                "event_count": 0,
            }
    core_discovery["eligible_count"] = len(core_eligible_rows)
    core_news_enrichment = enrich_candidate_news(
        core_eligible_rows,
        config=scanner_config,
        requested_at=cycle_decision_at,
        max_symbols=len(core_eligible_rows) or 1,
        rehearsal_mode=fixture_mode,
        out_dir=output_dir / "core_candidate_news",
    )
    core_discovery["candidate_news"] = core_news_enrichment["summary"]
    core_eligible_rows = list(core_news_enrichment.get("rows") or core_eligible_rows)
    enrichment = enrich_premarket_rows(
        list(collection.get("rows") or []),
        config=scanner_config,
        requested_at=cycle_decision_at,
        source=("yahoo" if fixture_mode else "alpaca"),
        allow_yahoo_fallback=not fixture_mode,
        rehearsal_mode=fixture_mode,
        out_dir=output_dir / "premarket_enrichment",
    )
    source_summary["premarket_enrichment"] = enrichment["summary"]
    news_enrichment = enrich_candidate_news(
        list(enrichment.get("ranking_rows") or []),
        config=scanner_config,
        requested_at=cycle_decision_at,
        max_symbols=scanner_config.premarket_enrichment_max_candidates,
        rehearsal_mode=fixture_mode,
        out_dir=output_dir / "candidate_news",
    )
    source_summary["candidate_news"] = dict(news_enrichment["summary"])
    enriched_snapshot_path = str(
        news_enrichment.get("snapshot_path")
        or dict(enrichment.get("paths") or {}).get("snapshot")
        or collection["snapshot_path"]
    )
    scan_result = ScanService(
        CsvSnapshotProvider(enriched_snapshot_path),
        store=store,
    ).run(scanner_config, persist=True, as_of=cycle_decision_at)
    scan_paths = write_scan_outputs(scan_result, scanner_config.output_dir)
    ranked = [candidate.to_dict() for candidate in scan_result.ranked_candidates]
    all_candidates = [candidate.to_dict() for candidate in scan_result.all_candidates]
    for row in [*ranked, *all_candidates]:
        row.setdefault("universe_lane", "mover")
        row.setdefault("evidence_lane", "mover")
    core_ranked: list[dict[str, Any]] = []
    core_all: list[dict[str, Any]] = []
    core_enrichment_summary: dict[str, Any] = {"status": "not_run"}
    if core_eligible_rows:
        core_snapshot = write_snapshot_rows(
            core_eligible_rows, output_dir / "web_collect" / "core_lane_snapshot.csv"
        )
        core_config = scanner_config.with_overrides(
            min_gap_pct=0.0,
            ideal_gap_low_pct=1.0,
            top_n=max(1, int(scanner_config.top_n)),
        )
        core_enrichment = enrich_premarket_rows(
            core_eligible_rows,
            config=core_config,
            requested_at=cycle_decision_at,
            source="alpaca",
            allow_yahoo_fallback=False,
            rehearsal_mode=False,
            out_dir=output_dir / "core_premarket_enrichment",
        )
        core_discovery["enrichment_summary"] = core_enrichment["summary"]
        core_enrichment_summary = dict(core_enrichment["summary"])
        core_snapshot_path = str(
            dict(core_enrichment.get("paths") or {}).get("snapshot") or core_snapshot
        )
        core_scan = ScanService(CsvSnapshotProvider(core_snapshot_path), store=store).run(
            core_config, persist=False, as_of=cycle_decision_at
        )
        core_ranked = [candidate.to_dict() for candidate in core_scan.ranked_candidates]
        core_all = [candidate.to_dict() for candidate in core_scan.all_candidates]
        for row in [*core_ranked, *core_all]:
            row["universe_lane"] = "core"
            row["evidence_lane"] = "core"
        lane_eligibility = {
            "mover": (
                not mover_source_failed
                and str(enrichment["summary"].get("status") or "").lower()
                in {"complete", "partial"}
            ),
            "core": (
                core_discovery_data_eligible(core_discovery)
                and str(core_enrichment_summary.get("status") or "").lower()
                in {"complete", "partial"}
            ),
        }
        ranked = _merge_lane_candidates(
            ranked, core_ranked, lane_eligibility=lane_eligibility
        )
        all_candidates = _merge_lane_candidates(
            all_candidates, core_all, lane_eligibility=lane_eligibility
        )
    core_ranked_count = sum(
        1 for row in ranked if str(row.get("universe_lane") or "") in {"core", "mover+core"}
    )
    mover_ranked_count = sum(
        1 for row in ranked if str(row.get("universe_lane") or "mover") in {"mover", "mover+core"}
    )
    core_eligible_count = int(core_discovery.get("eligible_count") or 0)
    mover_eligible_count = sum(
        1
        for row in all_candidates
        if str(row.get("universe_lane") or "mover") in {"mover", "mover+core"}
    )
    overlap_ranked_count = sum(1 for row in ranked if row.get("universe_lane") == "mover+core")
    source_summary["lane_counts"] = {
        "mover": {
            "member_count": int(source_summary.get("candidate_count") or mover_snapshot_count),
            "snapshot_count": mover_snapshot_count,
            "eligible_count": 0 if mover_source_failed else mover_eligible_count,
            "ranked_count": 0 if mover_source_failed else mover_ranked_count,
        },
        "core": {
            "member_count": int(core_universe.get("membership_count") or 0),
            "snapshot_count": int(core_discovery.get("returned_count") or 0),
            "eligible_count": core_eligible_count,
            "ranked_count": core_ranked_count,
        },
        "overlap": {"ranked_count": overlap_ranked_count},
    }
    timestamp = cycle_decision_timestamp
    scoring_cohort = _alpha_scoring_cohort(all_candidates, ranked)
    source_summary["alpha_scoring_cohort"] = {
        "status": "ALL_ALREADY_ENRICHED_ROWS",
        "total_input_count": len(all_candidates),
        "non_avoid_count": len(scoring_cohort),
        "avoided_count": max(0, len(all_candidates) - len(scoring_cohort)),
        "ranked_presentation_count": len(ranked),
        "reserve_enabled": True,
        "reserve_scope": (
            "all candidates produced from the already-enriched mover/core snapshots; "
            "downstream gates remain authoritative"
        ),
        "mover_enrichment_cap": int(scanner_config.premarket_enrichment_max_candidates),
        "provider_load_expanded": False,
        "residual_cap": (
            "mover collection/enrichment cap plus the independently returned core cohort"
        ),
    }
    scoring_cohort, ranked_sec_summary = _verify_ranked_sec_safety(
        scoring_cohort,
        source_config=source_config,
        store=store,
        out_dir=output_dir / "ranked_sec_safety",
        as_of=timestamp,
        rehearsal_mode=fixture_mode,
    )
    ranked = _merge_ranked_safety(ranked, scoring_cohort)
    source_summary["ranked_sec_safety"] = ranked_sec_summary
    all_candidates = _merge_ranked_safety(all_candidates, scoring_cohort)
    source_summary["alpha_scoring_cohort"].update(
        {
            "total_input_count": len(all_candidates),
            "non_avoid_count": len(scoring_cohort),
            "avoided_count": max(0, len(all_candidates) - len(scoring_cohort)),
            "sec_verified_count": len(
                set(ranked_sec_summary.get("checked_tickers") or [])
            ),
            "sec_unverified_count": len(
                set(ranked_sec_summary.get("unchecked_tickers") or [])
            ),
        }
    )
    reliability_by_source = {row["source"]: row for row in source_reliability}
    feature_vectors = [
        build_feature_vector(
            row,
            scan_id=scan_result.run_id,
            timestamp=timestamp,
            source_summary=source_summary,
            source_reliability=reliability_by_source,
        )
        for row in all_candidates
    ]
    store.persist_alpha_feature_vectors(feature_vectors)
    historical_labels = load_production_alpha_learning_labels(store)
    model = AlphaModel()
    signals = model.score_candidates(
        # ``ranked`` is intentionally a presentation slice capped at top_n
        # (20 in AlphaOps).  Score the full already-enriched cohort so safety
        # and plan gates can refill the five-name research slate from rows
        # below that presentation cutoff.  This does not load any additional
        # provider data and does not bypass downstream gates.
        scoring_cohort,
        feature_vectors,
        historical_outcomes=historical_labels,
        setup_memory=store.load_alpha_setup_memory(),
        real_shadow_days=_real_days(historical_labels),
    )
    signals = [
        _signal_payload(
            _attach_authenticated_alpaca_structure(row, decision_at=timestamp),
            scan_result.run_id,
            timestamp,
            index,
        )
        for index, row in enumerate(signals, 1)
    ]
    strategy_adapter_result = adapt_prior_session_paper_ops(
        output_root=paper_ops_root,
        market_date=timestamp[:10],
        current_candidates=all_candidates,
        current_snapshot_id=scan_result.run_id,
        current_source_identity=(
            str(source_summary.get("source_identity") or "").strip()
            or f"alpha_cycle:{scan_result.run_id}"
        ),
        current_code_sha=str(os.environ.get("DAWNSTRIKE_CODE_SHA") or "").strip(),
        current_universe_membership=[
            str(row.get("ticker") or row.get("symbol") or "").upper()
            for row in all_candidates
        ],
        current_core_membership=[
            str(row.get("symbol") or row.get("ticker") or "").upper()
            for row in core_universe.get("members") or []
        ],
        decision_at=timestamp,
    )
    signals.extend(
        dict(row)
        for row in list(strategy_adapter_result.get("rows") or [])
        if isinstance(row, dict)
    )
    strategy_receipt_stats = _apply_strategy_decision_receipts(
        signals,
        store=store,
        config=scanner_config,
        decision_at=timestamp,
        source_summary=source_summary,
    )
    # Receipts are built for every strategy contribution before this grouping;
    # the frozen slate then has one deterministic row per ticker while retaining
    # each contributor's identity and exact receipt lineage.
    signals = _merge_strategy_adapter_signals(signals, [])
    strategy_adapter_result["receipt_contributor_count"] = (
        _strategy_adapter_contributor_count(signals)
    )
    source_summary["morning_strategy_adapter"] = strategy_adapter_result
    if scanner_config.strategy_evidence_enabled:
        signals = _apply_receipt_risk_gates(signals, feature_vectors)
    signals = apply_alert_gates(signals)
    receipt_verifier = _persisted_strategy_receipt_verifier(
        store,
        market_date=timestamp[:10],
    )
    review = review_alpha_signals(signals, source_summary=source_summary)
    decision = dict(review["decision"])
    mover_enrichment_status = str(enrichment["summary"].get("status") or "").lower()
    core_snapshot_status = str(core_discovery.get("status") or "DATA_UNAVAILABLE").upper()
    core_enrichment_status = str(core_enrichment_summary.get("status") or "not_run").lower()
    mover_lane_eligible = (
        not mover_source_failed and mover_enrichment_status in {"complete", "partial"}
    )
    core_lane_eligible = (
        core_discovery_data_eligible(core_discovery)
        and core_enrichment_status in {"complete", "partial"}
    )
    coverage_limitations: list[str] = []
    if str(core_universe.get("status") or "") != "READY":
        coverage_limitations.append("core_membership_contract_unavailable")
    if core_snapshot_status != "READY":
        coverage_limitations.append("core_snapshot_coverage_incomplete")
    if not core_lane_eligible:
        coverage_limitations.append("core_enrichment_not_data_eligible")
    if mover_source_failed:
        coverage_limitations.append("mover_source_unavailable")
    mover_fallback_limited = str(
        enrichment["summary"].get("secondary_fallback_status") or ""
    ).lower() in {
        "research_only_applied_above_ceiling",
        "applied_research_only_above_ceiling",
        "ceiling_exceeded_not_applied",
    }
    if mover_fallback_limited:
        coverage_limitations.append("mover_secondary_fallback_above_ceiling")
    core_fallback_limited = str(
        core_enrichment_summary.get("secondary_fallback_status") or ""
    ).lower() in {
        "research_only_applied_above_ceiling",
        "applied_research_only_above_ceiling",
        "ceiling_exceeded_not_applied",
    }
    if core_fallback_limited:
        coverage_limitations.append("core_secondary_fallback_above_ceiling")
    combined_data_eligible = mover_lane_eligible or core_lane_eligible
    combined_coverage_status = (
        "COMPLETE"
        if combined_data_eligible and not coverage_limitations
        else "LIMITED"
        if combined_data_eligible
        else "DATA_UNAVAILABLE"
    )
    luna_research_slate = build_ranked_research_slate(
        signals,
        target=5,
        data_eligible=combined_data_eligible,
        generated_at=timestamp,
        market_date=timestamp[:10],
        scan_id=scan_result.run_id,
        canonical_member_ids=[
            str(row.get("symbol") or row.get("ticker") or "")
            for row in core_universe.get("members") or []
        ],
        require_safety=True,
        coverage_status=combined_coverage_status,
        lane_statuses={
            "mover": {
                "source_status": "SOURCE_FAILED" if mover_source_failed else "READY",
                "enrichment_status": mover_enrichment_status or "not_run",
                "data_eligible": mover_lane_eligible,
                "secondary_fallback_status": str(
                    enrichment["summary"].get("secondary_fallback_status") or ""
                ),
                "promotion_limited": mover_fallback_limited,
            },
            "core": {
                "contract_status": str(core_universe.get("status") or "DATA_UNAVAILABLE"),
                "snapshot_status": core_snapshot_status,
                "coverage_status": str(
                    core_discovery.get("coverage_status") or core_snapshot_status
                ).upper(),
                "snapshot_complete": core_snapshot_status == "READY",
                "snapshot_requested_count": int(core_discovery.get("requested_count") or 0),
                "snapshot_returned_count": int(core_discovery.get("returned_count") or 0),
                "snapshot_eligible_count": int(core_discovery.get("eligible_count") or 0),
                "snapshot_fresh_count": int(core_discovery.get("fresh_count") or 0),
                "snapshot_fresh_verified_count": int(
                    core_discovery.get("fresh_verified_count") or 0
                ),
                "snapshot_stale_count": int(core_discovery.get("stale_count") or 0),
                "snapshot_missing_count": int(core_discovery.get("missing_count") or 0),
                "snapshot_unknown_count": int(core_discovery.get("unknown_count") or 0),
                "snapshot_duplicate_count": int(core_discovery.get("duplicate_count") or 0),
                "coverage_receipt_ids": list(core_discovery.get("coverage_receipt_ids") or []),
                "coverage_receipt_hashes": list(
                    core_discovery.get("coverage_receipt_hashes") or []
                ),
                "coverage_limitations": list(core_discovery.get("limitations") or []),
                "enrichment_status": core_enrichment_status,
                "data_eligible": core_lane_eligible,
                "secondary_fallback_status": str(
                    core_enrichment_summary.get("secondary_fallback_status") or ""
                ),
                "promotion_limited": core_fallback_limited,
            },
        },
        coverage_limitations=coverage_limitations,
    )
    slate_path = output_dir / "ranked_research_slate.json"
    persist_ranked_research_slate(luna_research_slate, slate_path)
    luna_research_slate = _load_frozen_luna_slate(slate_path, market_date=timestamp[:10])
    source_summary["ranked_research_slate"] = luna_research_slate
    frozen_source_scan_id = str(luna_research_slate.get("scan_id") or "")
    source_summary["ranked_research_slate_lineage"] = {
        "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
        "slate_id": str(luna_research_slate.get("slate_id") or ""),
        "slate_content_hash_sha256": str(
            luna_research_slate.get("content_hash_sha256") or ""
        ),
        "frozen_source_scan_id": frozen_source_scan_id,
        "current_scan_id": scan_result.run_id,
        "reuse_status": (
            "CURRENT_SCAN"
            if frozen_source_scan_id == scan_result.run_id
            else "GOVERNED_DAILY_FREEZE_REUSE"
        ),
    }
    slate_publication_rows = apply_publication_semantics(
        list(luna_research_slate.get("rows") or []),
        slate=luna_research_slate,
        coverage={"lanes": luna_research_slate.get("lane_statuses") or {}},
        require_watcher_proof=True,
        receipt_verifier=receipt_verifier,
    )
    if len(slate_publication_rows) != int(luna_research_slate.get("published_count") or 0):
        raise SnapshotValidationError(
            "FROZEN_SLATE_SIGNAL_MISSING: immutable research rows could not be "
            "reconstructed for publication"
        )
    source_summary["ranked_research_publication_rows"] = slate_publication_rows
    signals = apply_publication_semantics(
        signals,
        slate=luna_research_slate,
        coverage={"lanes": luna_research_slate.get("lane_statuses") or {}},
        require_watcher_proof=True,
        receipt_verifier=receipt_verifier,
    )
    # The persisted daily slate is authoritative.  An empty frozen slate is a
    # deliberate no-research result and must not be repopulated from the
    # current signal set during a retry or no-edge rendering path.
    research_radar = (
        _research_radar(signals)
        if decision.get("no_trade")
        and int(luna_research_slate.get("published_count") or 0) > 0
        else []
    )
    signals = _annotate_research_radar(signals, research_radar)
    store.persist_alpha_signals(signals)
    regime = detect_regime(signals, source_summary)
    v6_universe_registration = _register_alpaca_screening_universe(
        store,
        source_summary=source_summary,
        market_date=timestamp[:10],
    )
    universe_memberships = active_alpha_v6_membership_by_ticker(
        store,
        market_date=timestamp[:10],
        tickers=[str(row.get("ticker") or "") for row in all_candidates],
    )
    candidate_tickers = {
        str(row.get("ticker") or "").upper() for row in all_candidates if row.get("ticker")
    }
    missing_v6_universe_memberships = sorted(candidate_tickers - set(universe_memberships))
    if (
        isinstance(source_summary.get("production_contract"), dict)
        and source_summary["production_contract"].get("status") == "READY"
        and missing_v6_universe_memberships
    ):
        raise SnapshotValidationError(
            "Production V6 universe coverage is incomplete; refusing to create "
            "shadow decisions without point-in-time membership. Missing: "
            + ", ".join(missing_v6_universe_memberships[:20])
        )
    v6_model_runs = store.load_alpha_v6_model_runs(limit=1)
    v6_decisions = build_candidate_decisions(
        signals=signals,
        candidates=all_candidates,
        feature_vectors=feature_vectors,
        source_summary=source_summary,
        regime=regime,
        prior_outcomes=store.load_alpha_v6_outcomes(),
        frozen_model_run=v6_model_runs[0] if v6_model_runs else None,
        decision_at=timestamp,
        scan_id=scan_result.run_id,
        universe_membership_by_ticker=universe_memberships,
    )
    v6_decision_stats = store.persist_alpha_v6_decisions(v6_decisions)
    selected_signals = official_publication_rows(slate_publication_rows, limit=3)
    source_summary["official_publication_rows"] = selected_signals
    official_no_trade = not selected_signals
    publication_decision = dict(decision)
    if official_no_trade:
        publication_reason = str(decision.get("reason") or "").strip()
        if not publication_reason or not decision.get("no_trade"):
            publication_reason = (
                "No immutable frozen Tier 2/3 plan qualified for official paper publication."
            )
        publication_decision.update(
            {
                "no_trade": True,
                "decision_tier": "no_trade",
                "reason": publication_reason,
                "primary_reason_code": "frozen_slate_no_official_selection",
                "next_action": str(decision.get("next_action") or "").strip()
                or "Review the ranked research slate and wait for all plan gates to pass.",
            }
        )
    else:
        publication_decision.update(
            {
                "no_trade": False,
                "decision_tier": "clean_edge",
                "reason": "Immutable frozen Tier 2/3 paper-plan cohort selected.",
                "primary_reason_code": "frozen_slate_official_selection",
            }
        )
    publication_review = {
        **review,
        "decision": publication_decision,
        "watchlist": selected_signals,
    }
    no_trade_row: dict[str, Any] | None = None
    if official_no_trade:
        no_trade_row = _build_no_trade_historical_signal(
            scan_id=scan_result.run_id,
            generated_at=timestamp,
            reason=str(publication_decision.get("reason") or ""),
            source_summary=source_summary,
            candidate_count=len(ranked),
        )
    if official_no_trade:
        frozen_manifest = _load_frozen_official_notification_manifest(
            store, selected_at=timestamp
        )
        message = (
            str(frozen_manifest.get("body") or "")
            if frozen_manifest is not None
            else format_alpha_no_trade(
                reason=str(publication_decision.get("reason") or ""),
                next_action=str(publication_decision.get("next_action") or ""),
                research_signals=slate_publication_rows,
                target_count=int(luna_research_slate.get("target_count") or 0),
                published_count=int(luna_research_slate.get("published_count") or 0),
                slate_shortfall_reason=str(
                    luna_research_slate.get("slate_shortfall_reason") or ""
                ),
            )
        )
        hint = "alpha_no_trade"
        title = "Dawnstrike Alpha Check"
    else:
        edge_label = (
            _trust_gate_edge_label(selected_signals)
            if str(decision.get("decision_tier") or "") == "probability_fallback"
            else _edge_label(selected_signals)
        )
        frozen_manifest = _load_frozen_official_notification_manifest(
            store, selected_at=timestamp
        )
        message = (
            str(frozen_manifest.get("body") or "")
            if frozen_manifest is not None
            else format_alpha_watch(
                signals=slate_publication_rows,
                edge_label=edge_label,
                source_summary=source_summary,
                blocked_signals=list(review["blocked"]),
                generated_at=timestamp,
                target_count=int(luna_research_slate.get("target_count") or 0),
                published_count=int(luna_research_slate.get("published_count") or 0),
                slate_shortfall_reason=str(
                    luna_research_slate.get("slate_shortfall_reason") or ""
                ),
            )
        )
        hint = "alpha_morning_watch"
        title = "Dawnstrike Alpha Watch"
    events = [
        _official_selection_notification_event(
            scan_result.run_id,
            hint,
            title,
            message,
            selected_signals=selected_signals,
            research_signals=slate_publication_rows,
        )
    ]
    selection_members = selected_signals or ([no_trade_row] if no_trade_row is not None else [])
    frozen_cohort_retry = _govern_frozen_official_cohort_retry(
        store,
        scan_id=scan_result.run_id,
        selected_signals=selection_members,
        decision=publication_decision,
        selected_at=timestamp,
        event=events[0],
    )
    if frozen_cohort_retry is None:
        selected_rows, selection_stats = _persist_official_selections(
            store,
            scan_id=scan_result.run_id,
            selected_signals=selection_members,
            decision=publication_decision,
            selected_at=timestamp,
            event=events[0],
            slate=luna_research_slate,
        )
        selection_scan_id = scan_result.run_id
        selection_selected_at = timestamp
    else:
        events = [frozen_cohort_retry["event"]]
        selected_rows = list(frozen_cohort_retry["selections"])
        selection_stats = dict(frozen_cohort_retry["stats"])
        selection_scan_id = str(frozen_cohort_retry["scan_id"])
        selection_selected_at = str(frozen_cohort_retry["selected_at"])
    radar_selection_rows, radar_selection_stats = _persist_research_radar_selections(
        store,
        scan_id=selection_scan_id,
        # Freeze the exact five-target research slate for monitoring on both
        # edge and no-edge days.  The radar cohort is the existing read-only
        # persistence surface and remains broker-disabled.
        radar=list(luna_research_slate.get("rows") or []),
        slate=luna_research_slate,
        selected_at=selection_selected_at,
        event=events[0],
    )
    official_signal_ids = {
        str(row.get("signal_id") or "") for row in selected_rows
    }
    delivery_radar_rows = [
        row
        for row in radar_selection_rows
        if str(row.get("signal_id") or "") not in official_signal_ids
    ]
    radar_selection_stats["delivery_overlap_excluded"] = len(
        radar_selection_rows
    ) - len(delivery_radar_rows)
    delivery_selection_rows = [*selected_rows, *delivery_radar_rows]
    if frozen_cohort_retry is None:
        historical_rows = record_alpha_historical_signals(
            store,
            _historical_publication_rows(signals, slate_publication_rows),
            source_summary=source_summary,
            no_trade_reason=(
                str(publication_decision.get("reason") or "") if official_no_trade else ""
            ),
        )
        if official_no_trade:
            record_no_trade_historical_signal(
                store,
                scan_id=scan_result.run_id,
                generated_at=timestamp,
                reason=str(publication_decision.get("reason") or ""),
                source_summary=source_summary,
                candidate_count=len(ranked),
            )
    else:
        frozen_signal_ids = {
            str(row.get("signal_id") or "")
            for row in delivery_selection_rows
            if str(row.get("signal_id") or "")
        }
        historical_rows = [
            row
            for row in store.load_historical_signals(
                scan_id=selection_scan_id,
                limit=max(100, len(frozen_signal_ids) * 2, 1),
            )
            if str(row.get("signal_id") or "") in frozen_signal_ids
        ]
    selected_signal_ids = [str(row["signal_id"]) for row in delivery_selection_rows]
    preexisting_notification_keys = _existing_notification_keys(
        store,
        events=events,
        notify=notify,
    )
    cycle_contract_args: dict[str, Any] = {
        "scan_id": scan_result.run_id,
        "generated_at": timestamp,
        "ranked_count": len(ranked),
        "signals": signals,
        "review": publication_review,
        "source_summary": source_summary,
        "enrichment_summary": dict(enrichment["summary"]),
        "receipt_verifier": receipt_verifier,
    }
    _persist_run_contract(
        output_dir,
        **cycle_contract_args,
        notification_stats={},
        notification_channel=notify,
        notification_dry_run=dry_run,
        notification_status_override="pending",
    )
    try:
        notification_stats = _dispatch(
            events,
            notify=notify,
            db_path=db_path,
            dry_run=dry_run,
        )
    except Exception:
        _persist_notification_delivery_memberships(
            store,
            selections=delivery_selection_rows,
            events=events,
            notify=notify,
            preexisting_notification_keys=preexisting_notification_keys,
        )
        _persist_run_contract(
            output_dir,
            **cycle_contract_args,
            notification_stats={},
            notification_channel=notify,
            notification_dry_run=dry_run,
            notification_status_override="delivery_failed",
        )
        raise
    notification_deliveries = _persist_notification_delivery_memberships(
        store,
        selections=delivery_selection_rows,
        events=events,
        notify=notify,
        preexisting_notification_keys=preexisting_notification_keys,
    )
    run_contract = _persist_run_contract(
        output_dir,
        **cycle_contract_args,
        notification_stats=notification_stats,
        notification_channel=notify,
        notification_dry_run=dry_run,
    )
    notification_link = _link_notification_events(
        store,
        scan_id=selection_scan_id,
        events=events,
        notify=notify,
        dry_run=dry_run,
        signal_ids=selected_signal_ids,
        notification_deliveries=notification_deliveries,
    )
    result: dict[str, Any] = {
        "status": "no_trade" if official_no_trade else "complete",
        "run_type": cycle_name,
        "scan_id": scan_result.run_id,
        "model_version": ALPHA_MODEL_VERSION,
        "source_summary": source_summary,
        "source_reliability": source_reliability,
        "premarket_enrichment": enrichment["summary"],
        "regime": regime,
        "feature_vector_count": len(feature_vectors),
        "v6_shadow": {
            "strategy_version": "dawnstrike-alphaops-v6-shadow",
            "decision_count": len(v6_decisions),
            "tracked_count": sum(1 for row in v6_decisions if row.get("action") == "SHADOW_TRACK"),
            "persistence": v6_decision_stats,
            "versioned_universe_membership_count": len(universe_memberships),
            "missing_versioned_universe_memberships": missing_v6_universe_memberships,
            "universe_registration": v6_universe_registration,
            "research_only": True,
            "broker_execution_enabled": False,
        },
        "signal_count": len(signals),
        "strategy_decision_receipts": strategy_receipt_stats,
        "research_radar": research_radar,
        "ranked_research_slate": luna_research_slate,
        "core_universe": core_universe,
        "historical_signal_count": len(historical_rows),
        "historical_notification_link": notification_link,
        "top_signal": signals[0] if signals else None,
        "review": publication_review,
        "selection_stats": selection_stats,
        "research_radar_selection_stats": radar_selection_stats,
        "notification_stats": notification_stats,
        "notification_deliveries": notification_deliveries,
        "run_contract": run_contract.to_dict(),
        "scan_paths": {key: str(value) for key, value in scan_paths.items()},
        "out_dir": str(output_dir),
    }
    if session_gate is not None:
        result["session_gate"] = session_gate.to_dict()
    _write_json(output_dir / "alpha_cycle.json", result)
    _write_json(output_dir / "alpha_signals.json", signals)
    _write_json(output_dir / "alpha_features.json", feature_vectors)
    return result


def alpha_monitor(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    notify: str = "console",
    dry_run: bool = False,
    current_prices: dict[str, float] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    session_gate = _scheduled_session_gate(notify=notify, as_of=as_of)
    if session_gate is not None:
        phase = core_session_phase(as_of)
        if not session_gate.is_trading_day:
            return _session_skip_result(
                run_type="alpha_monitor",
                status="skipped_market_closed",
                decision=session_gate,
            )
        if phase != "core_session_open":
            return _session_skip_result(
                run_type="alpha_monitor",
                status="skipped_outside_core_session",
                decision=session_gate,
                phase=phase,
            )
    store = SQLiteScanStore(db_path)
    latest_attempt_signals = store.load_alpha_signals(limit=25)
    latest_attempt_scan_id = (
        str(latest_attempt_signals[0].get("scan_id") or "")
        if latest_attempt_signals
        else ""
    )
    latest_attempt_signals = [
        row
        for row in latest_attempt_signals
        if str(row.get("scan_id") or "") == latest_attempt_scan_id
    ]
    latest_attempt_date = (
        _signal_market_date(latest_attempt_signals[0])
        if latest_attempt_signals
        else ""
    )
    required_market_date = (
        session_gate.market_date if session_gate is not None else latest_attempt_date
    )
    contract_reference: str | datetime = (
        as_of
        or (
            str(latest_attempt_signals[0].get("timestamp") or "")
            if latest_attempt_signals
            else ""
        )
        or f"{required_market_date}T12:00:00+00:00"
    )
    strategy_id, strategy_version = alphaops_strategy_contract(contract_reference)
    official_cohort = (
        store.load_official_strategy_cohort(
            market_date=required_market_date,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            cohort=ALPHAOPS_OFFICIAL_COHORT,
        )
        if required_market_date
        else None
    )
    # A governed Morning retry may persist a newer producer scan while the
    # date-level official cohort deliberately remains frozen on its original
    # scan.  Monitoring follows the authoritative cohort, never whichever
    # alpha_signal row happened to be written most recently.
    latest_scan_id = str(
        (official_cohort or {}).get("scan_id") or latest_attempt_scan_id
    )
    latest_signal_date = str(
        (official_cohort or {}).get("market_date") or latest_attempt_date
    )[:10]
    signals = (
        store.load_alpha_signals(scan_id=latest_scan_id, limit=500)
        if latest_scan_id
        else []
    )
    if not signals and official_cohort is None:
        signals = latest_attempt_signals
    if session_gate is not None and signals and latest_signal_date != session_gate.market_date:
        return {
            "status": "stale_watchlist",
            "label": "NO CURRENT WATCH",
            "message": (
                "The latest saved AlphaOps watchlist is not from the current market "
                "session, so no monitor notification was created."
            ),
            "latest_watchlist_market_date": latest_signal_date or "unknown",
            "required_market_date": session_gate.market_date,
            "tickers": [],
            "events": [],
            "notification_stats": {"sent": 0, "skipped": 0},
            "session_gate": session_gate.to_dict(),
        }
    exact_selections = store.load_signal_selections(
        scan_id=latest_scan_id,
        event_key=str((official_cohort or {}).get("event_key") or "") or None,
        strategy_id=strategy_id if official_cohort is not None else None,
        cohort=ALPHAOPS_OFFICIAL_COHORT,
        limit=500,
    )
    if (
        official_cohort is None
        and session_gate is not None
        and latest_scan_id
        and not exact_selections
    ):
        # Older AlphaOps runs predate the immutable selection tables.  Recovery
        # is deliberately based on the exact persisted Telegram body; an
        # ambiguous/truncated message remains unknown and therefore fail-closed.
        recover_legacy_alpha_notification_memberships(db_path=db_path, limit=500)
        exact_selections = store.load_signal_selections(
            scan_id=latest_scan_id,
            cohort=ALPHAOPS_OFFICIAL_COHORT,
            limit=500,
        )
    radar_selections = store.load_signal_selections(
        scan_id=latest_scan_id,
        event_key=str((official_cohort or {}).get("event_key") or "") or None,
        strategy_id=ALPHAOPS_RADAR_COHORT if official_cohort is not None else None,
        cohort=ALPHAOPS_RADAR_COHORT,
        limit=50,
    )
    try:
        if official_cohort is not None:
            if not _valid_monitor_official_cohort(
                official_cohort,
                exact_selections,
                market_date=required_market_date,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
            ):
                raise SnapshotValidationError(
                    "The persisted official cohort, message manifest, or membership "
                    "does not match its immutable date-level identity."
                )
            if session_gate is not None:
                delivered = validate_or_recover_official_cohort(
                    store,
                    market_date=required_market_date,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    persist_recovery=False,
                )
                if delivered.errors:
                    raise SnapshotValidationError(
                        "The official cohort lacks exact Telegram delivery proof: "
                        + "; ".join(delivered.errors)
                    )
        official_monitor_signals = _official_monitor_signals(
            signals,
            exact_selections,
            receipt_verifier=_persisted_strategy_receipt_verifier(
                store,
                market_date=required_market_date,
            ),
        )
        radar_monitor_signals = _radar_monitor_signals(signals, radar_selections)
    except SnapshotValidationError as exc:
        return {
            "status": "selection_evidence_unavailable",
            "label": "SELECTION AUDIT REQUIRED",
            "message": str(exc),
            "latest_watchlist_market_date": latest_signal_date or "unknown",
            "required_market_date": session_gate.market_date if session_gate else None,
            "tickers": [],
            "events": [],
            "notification_stats": {"sent": 0, "skipped": 0},
            "selection_evidence_status": "unavailable",
            "session_gate": session_gate.to_dict() if session_gate else None,
        }
    if radar_selections and not radar_monitor_signals:
        return {
            "status": "selection_evidence_unavailable",
            "label": "SELECTION AUDIT REQUIRED",
            "message": (
                "The persisted research slate selections could not be matched to "
                "current or governed frozen signal rows; no monitor notification was created."
            ),
            "latest_watchlist_market_date": latest_signal_date or "unknown",
            "required_market_date": session_gate.market_date if session_gate else None,
            "tickers": [],
            "events": [],
            "notification_stats": {"sent": 0, "skipped": 0},
            "selection_evidence_status": "unavailable",
            "session_gate": session_gate.to_dict() if session_gate else None,
        }
    selection_evidence_status = "legacy_manual_fallback"
    if exact_selections:
        official_signal_ids = {
            str(row.get("signal_id") or "")
            for row in exact_selections
            if str(row.get("decision") or "").lower() != "no_trade"
            and str(row.get("ticker") or "").upper() != "NO_TRADE"
        }
        if official_signal_ids:
            if {
                str(row.get("signal_id") or row.get("signal_key") or "")
                for row in official_monitor_signals
            } != official_signal_ids:
                return {
                    "status": "selection_evidence_unavailable",
                    "label": "SELECTION AUDIT REQUIRED",
                    "message": (
                        "The exact official AlphaOps cohort could not be reconstructed; "
                        "no monitor notification was created."
                    ),
                    "latest_watchlist_market_date": latest_signal_date or "unknown",
                    "required_market_date": (
                        session_gate.market_date if session_gate else None
                    ),
                    "tickers": [],
                    "events": [],
                    "notification_stats": {"sent": 0, "skipped": 0},
                    "selection_evidence_status": "unavailable",
                    "session_gate": session_gate.to_dict() if session_gate else None,
                }
            radar_monitor_signals = [
                row
                for row in radar_monitor_signals
                if str(row.get("signal_id") or row.get("signal_key") or "")
                not in official_signal_ids
            ]
            signals = [*official_monitor_signals, *radar_monitor_signals]
            selection_evidence_status = "exact_official_and_research_slate_cohort"
        elif radar_selections:
            signals = radar_monitor_signals
            selection_evidence_status = "exact_research_radar_cohort"
        else:
            signals = []
            selection_evidence_status = "exact_no_trade_cohort"
    elif radar_selections:
        signals = radar_monitor_signals
        selection_evidence_status = "exact_research_radar_cohort"
    elif session_gate is not None and signals:
        return {
            "status": "selection_evidence_unavailable",
            "label": "SELECTION AUDIT REQUIRED",
            "message": (
                "The exact delivered AlphaOps cohort could not be proven, so no "
                "signals were monitored and no paper lifecycle was inferred."
            ),
            "latest_watchlist_market_date": latest_signal_date or "unknown",
            "required_market_date": session_gate.market_date,
            "tickers": [],
            "events": [],
            "notification_stats": {"sent": 0, "skipped": 0},
            "selection_evidence_status": "unavailable",
            "session_gate": session_gate.to_dict(),
        }
    active_signals = [
        row
        for row in signals
        if (
            str(row.get("monitor_cohort") or "") == ALPHAOPS_RADAR_COHORT
            or (bool(row.get("can_alert")) and not str(row.get("no_trade_reason") or "").strip())
        )
    ]
    price_observation: dict[str, Any] | None = None
    current_quotes: dict[str, dict[str, Any]] | None = None
    if current_prices is None and active_signals:
        try:
            price_observation = collect_price_observations(
                db_path=db_path,
                source="alpaca",
                tickers=[str(row.get("ticker") or "") for row in active_signals],
                max_age_seconds=360,
                persist=False,
            )
        except DataProviderError:
            if not dry_run:
                raise
            price_observation = {
                "status": "provider_unavailable_dry_run",
                "usable_count": 0,
                "observations": [],
                "research_only": True,
                "broker_execution_enabled": False,
            }
        current_prices = {
            str(row.get("ticker") or "").upper(): float(row["current_price"])
            for row in price_observation.get("observations", [])
            if row.get("is_usable") and row.get("current_price") not in {None, ""}
        }
        try:
            live_config = load_config(database_path=Path(db_path))
            quote_provider = AlpacaProvider(live_config)
            quote_provider.validate_credentials()
            quote_rows = quote_provider.get_latest_quotes(
                [str(row.get("ticker") or "") for row in active_signals],
                live_config,
            )
            current_quotes = {
                ticker: {
                    **row,
                    "is_usable": _live_quote_is_usable(row, as_of=as_of),
                }
                for ticker, row in quote_rows.items()
            }
        except DataProviderError as exc:
            current_quotes = {}
            if price_observation is not None:
                price_observation["quote_status"] = "provider_unavailable"
                price_observation["quote_error"] = str(exc)
    if not active_signals:
        result: dict[str, Any] = {
            "status": "no_active_watchlist",
            "label": "NO ACTIVE WATCH",
            "message": "The latest AlphaOps run has no alertable research watchlist to monitor.",
            "tickers": [],
            "events": [],
        }
    else:
        result = monitor_alpha_signals(
            active_signals,
            current_prices=current_prices,
            current_quotes=current_quotes,
        )
    if price_observation is not None:
        result["price_observation"] = {
            key: value for key, value in price_observation.items() if key != "observations"
        }
        if not current_prices:
            result.update(
                {
                    "status": "manual_monitor_required",
                    "label": "MANUAL REVIEW",
                    "message": (
                        "No fresh sourced current prices were available for the "
                        "active watchlist; manual review is required."
                    ),
                    "events": [],
                    "tickers": [
                        str(row.get("ticker") or "")
                        for row in active_signals
                        if str(row.get("ticker") or "")
                    ],
                }
            )
    if current_quotes is not None:
        result["live_quote_check"] = {
            "required_for_research_radar": True,
            "usable_count": sum(1 for row in current_quotes.values() if row.get("is_usable")),
            "requested_count": len(active_signals),
            "maximum_spread_pct": 3.0,
        }
    result["historical_event_stats"] = record_monitor_signal_events(
        store,
        signals=active_signals,
        monitor_events=list(result.get("events") or []),
    )
    events: list[NotificationEvent] = []
    if result.get("status") != "no_active_watchlist":
        message = format_alpha_monitor(result)
        event_key = _monitor_event_key(latest_scan_id, result)
        events.append(
            _notification_event("alpha_monitor", event_key, "Dawnstrike Alpha Monitor", message)
        )
    result["notification_stats"] = _dispatch(
        events,
        notify=notify,
        db_path=db_path,
        dry_run=dry_run,
    )
    if session_gate is not None:
        result["session_gate"] = session_gate.to_dict()
    result["selection_evidence_status"] = selection_evidence_status
    return result


def _monitor_event_key(scan_id: str, result: dict[str, Any]) -> str:
    states = sorted(
        f"{row.get('ticker')}:{row.get('status') or row.get('label')}"
        for row in result.get("events", [])
    )
    state_text = ";".join(states) or str(result.get("status") or "unknown")
    digest = hashlib.sha256(f"{scan_id}|{state_text}".encode()).hexdigest()[:16]
    return f"{scan_id or 'no-scan'}:{digest}"


def _valid_monitor_official_cohort(
    cohort: dict[str, Any],
    selections: list[dict[str, Any]],
    *,
    market_date: str,
    strategy_id: str,
    strategy_version: str,
) -> bool:
    """Bind monitor ownership to the exact frozen message and member set."""

    identity = f"{market_date}|{strategy_id}|{strategy_version}|{ALPHAOPS_OFFICIAL_COHORT}"
    expected_cohort_id = (
        "official-cohort:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    )
    if (
        not selections
        or str(cohort.get("official_cohort_id") or "") != expected_cohort_id
        or str(cohort.get("market_date") or "") != market_date
        or str(cohort.get("strategy_id") or "") != strategy_id
        or str(cohort.get("strategy_version") or "") != strategy_version
        or str(cohort.get("cohort") or "") != ALPHAOPS_OFFICIAL_COHORT
        or membership_sha256(selections)
        != str(cohort.get("membership_sha256") or "")
    ):
        return False
    for selection in selections:
        if any(
            str(selection.get(field) or "") != str(cohort.get(field) or "")
            for field in (
                "scan_id",
                "event_key",
                "body_sha256",
                "strategy_id",
                "strategy_version",
                "cohort",
            )
        ) or str(selection.get("selected_at") or "")[:10] != market_date:
            return False
    payload = cohort.get("payload_json")
    manifest = payload.get("notification_manifest") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(manifest, dict):
        return False
    try:
        member_count = int(payload.get("member_count") or 0)
    except (TypeError, ValueError):
        return False
    body = str(manifest.get("body") or "")
    return bool(
        payload.get("research_only") is True
        and payload.get("broker_execution_enabled") is False
        and member_count == len(selections)
        and sorted(str(value) for value in payload.get("selection_ids") or [])
        == sorted(str(row.get("selection_id") or "") for row in selections)
        and sorted(str(value) for value in payload.get("signal_ids") or [])
        == sorted(str(row.get("signal_id") or "") for row in selections)
        and manifest.get("schema_version")
        == "dawnstrike.alphaops.official_notification_manifest.v1"
        and str(manifest.get("event_key") or "") == str(cohort.get("event_key") or "")
        and str(manifest.get("body_sha256") or "")
        == str(cohort.get("body_sha256") or "")
        and _body_sha256(body) == str(cohort.get("body_sha256") or "")
        and str(manifest.get("title") or "")
        and str(manifest.get("channel_hint") or "")
        and manifest.get("research_only") is True
        and manifest.get("broker_execution_enabled") is False
    )


def _official_monitor_signals(
    signals: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    *,
    receipt_verifier: AuthenticatedStrategyReceiptResolver | None = None,
) -> list[dict[str, Any]]:
    """Rehydrate the exact official cohort, including governed scan retries."""

    signal_by_id = {
        str(row.get("signal_id") or row.get("signal_key") or ""): row
        for row in signals
        if str(row.get("signal_id") or row.get("signal_key") or "")
    }
    monitored: list[dict[str, Any]] = []
    for selection in selections:
        if (
            str(selection.get("decision") or "").lower() == "no_trade"
            or str(selection.get("ticker") or "").upper() == "NO_TRADE"
        ):
            continue
        signal_id = str(selection.get("signal_id") or "")
        selection_scan_id = str(selection.get("scan_id") or "")
        source_scan_id = str(selection.get("source_scan_id") or "")
        if not signal_id or not selection_scan_id:
            raise SnapshotValidationError(
                "Official selection is missing immutable signal or scan identity."
            )
        signal = validated_frozen_selection_signal(
            selection,
            market_date=str(selection.get("selected_at") or "")[:10],
            allowed_cohorts=(ALPHAOPS_OFFICIAL_COHORT,),
        )
        if signal is not None:
            payload = selection.get("payload_json")
            slate = payload.get("frozen_ranked_research_slate") if isinstance(
                payload, dict
            ) else None
            if not isinstance(slate, dict):  # pragma: no cover - validator requires it
                raise SnapshotValidationError(
                    "Official selection is missing its frozen research slate."
                )
            # Re-derive Tier 2/3 annotations from the validated frozen source
            # and persisted decision receipt.  The selection's publication_row
            # is audit material, not trusted executable state.
            signal = apply_publication_semantics(
                [signal],
                slate=slate,
                coverage={"lanes": slate.get("lane_statuses") or {}},
                require_watcher_proof=True,
                receipt_verifier=receipt_verifier,
            )[0]
            if str(signal.get("publication_tier") or "") not in {
                "PAPER_PLAN_QUALIFIED",
                "ALERTABLE_PAPER_ENTRY",
            }:
                raise SnapshotValidationError(
                    "Official selection no longer satisfies its frozen paper-plan boundary."
                )
        else:
            # Compatibility for older same-scan cohorts that predate frozen
            # slate payloads.  A cross-scan assertion never receives this
            # fallback because it would allow retry-time signal replacement.
            if source_scan_id and source_scan_id != selection_scan_id:
                raise SnapshotValidationError(
                    "Official selection has invalid governed frozen-slate lineage."
                )
            candidate = signal_by_id.get(signal_id)
            if (
                candidate is None
                or str(candidate.get("scan_id") or "") != selection_scan_id
                or str(candidate.get("ticker") or "").upper()
                != str(selection.get("ticker") or "").upper()
            ):
                raise SnapshotValidationError(
                    "Official selection could not be matched to its exact persisted signal."
                )
            signal = dict(candidate)
        monitored.append(
            {
                **signal,
                "monitor_cohort": ALPHAOPS_OFFICIAL_COHORT,
                "research_only": True,
                "broker_execution_enabled": False,
            }
        )
    return monitored


def _radar_monitor_signals(
    signals: list[dict[str, Any]],
    selections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    signal_by_id = {
        str(row.get("signal_id") or row.get("signal_key") or ""): row
        for row in signals
        if str(row.get("signal_id") or row.get("signal_key") or "")
    }
    monitored: list[dict[str, Any]] = []
    for selection in selections:
        signal_id = str(selection.get("signal_id") or "")
        if not signal_id:
            raise SnapshotValidationError("Research slate selection is missing signal identity.")
        selection_payload = dict(selection.get("payload_json") or {})
        radar_signal = dict(selection_payload.get("signal") or {})
        selection_scan_id = str(selection.get("scan_id") or "")
        source_scan_id = str(selection.get("source_scan_id") or "")
        cross_scan = bool(source_scan_id and source_scan_id != selection_scan_id)
        if cross_scan:
            radar_signal = _validated_frozen_radar_signal(selection)
            if radar_signal is None:
                raise SnapshotValidationError(
                    "Persisted research slate selection has invalid governed frozen-slate lineage."
                )
            signal = radar_signal
        else:
            signal = signal_by_id.get(signal_id)
            if signal is None:
                if not radar_signal:
                    continue
                validated = _validated_frozen_radar_signal(selection)
                if validated is None:
                    continue
                signal = validated
        target = _number(radar_signal.get("radar_target") or signal.get("research_radar_target"))
        monitored.append(
            {
                **signal,
                "monitor_cohort": ALPHAOPS_RADAR_COHORT,
                "monitor_strategy_version": ALPHAOPS_RADAR_VERSION,
                "target_1": target or signal.get("target_1"),
                "first_target": target or signal.get("first_target"),
                "research_radar_target": target,
                "research_only": True,
                "broker_execution_enabled": False,
            }
        )
    return monitored


def _validated_frozen_radar_signal(selection: dict[str, Any]) -> dict[str, Any] | None:
    """Return the exact slate row carried by a governed radar selection."""

    payload = selection.get("payload_json")
    if not isinstance(payload, dict):
        return None
    slate = payload.get("frozen_ranked_research_slate")
    if not isinstance(slate, dict):
        return None
    market_date = str(selection.get("selected_at") or slate.get("market_date") or "")[:10]
    return validated_frozen_selection_signal(
        selection,
        market_date=market_date,
        allowed_cohorts=(ALPHAOPS_RADAR_COHORT,),
    )


def _live_quote_is_usable(
    quote: dict[str, Any],
    *,
    as_of: datetime | None,
) -> bool:
    bid = _number(quote.get("bid"))
    ask = _number(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return False
    raw_timestamp = str(quote.get("timestamp") or "").strip()
    if not raw_timestamp:
        return False
    try:
        observed_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    reference = as_of.astimezone(timezone.utc) if as_of else datetime.now(timezone.utc)
    age_seconds = (reference - observed_at.astimezone(timezone.utc)).total_seconds()
    return -5.0 <= age_seconds <= 120.0


def _scheduled_session_gate(
    *,
    notify: str,
    as_of: datetime | None,
) -> MarketSessionDecision | None:
    channels = {channel.strip().lower() for channel in notify.split(",") if channel.strip()}
    if not channels or channels <= {"console"}:
        return None
    return session_for_timestamp(as_of)


def _session_skip_result(
    *,
    run_type: str,
    status: str,
    decision: MarketSessionDecision,
    phase: str = "market_closed",
) -> dict[str, Any]:
    return {
        "status": status,
        "run_type": run_type,
        "phase": phase,
        "message": "No external research notification was attempted outside its allowed session.",
        "session_gate": decision.to_dict(),
        "notification_stats": {"sent": 0, "skipped": 0, "errors": []},
        "research_only": True,
        "order_execution_enabled": False,
    }


def alpha_outcomes(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    return run_alpha_learning(store)


def alpha_learn(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    return run_alpha_learning(store)


def alpha_status(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    store = SQLiteScanStore(db_path, read_only=True)
    store.initialize()
    latest_scan = store.load_latest_scan()
    signals = store.load_alpha_signals(limit=20)
    labels = load_production_alpha_learning_labels(store)
    learning = store.load_alpha_learning_runs(limit=1)
    reliability = store.load_alpha_source_reliability()
    setup_memory = store.load_alpha_setup_memory()
    real_days = _real_days(labels)
    latest_market_date = _latest_signal_date(store)
    outcome_status = calculate_missing_outcome_status(db_path, latest_market_date)
    return {
        "status": "ok",
        "db_path": str(db_path),
        "latest_scan_id": dict(latest_scan or {}).get("run_id"),
        "model_version": ALPHA_MODEL_VERSION,
        "signal_count": len(signals),
        "latest_signal": signals[0] if signals else None,
        "feature_vector_count": len(store.load_alpha_feature_vectors(limit=5000)),
        "outcome_label_count": len(labels),
        "source_reliability_count": len(reliability),
        "setup_memory_count": len(setup_memory),
        "real_days_collected": real_days,
        "missing_outcome_status": outcome_status,
        "missing_outcome_count": outcome_status.get("missing_outcome_count", 0),
        "enough_evidence": real_days >= 20,
        "last_learning_run": _normalize_learning_run(learning[0]) if learning else None,
        "research_only": True,
        "order_execution_enabled": False,
    }


def _latest_signal_date(store: SQLiteScanStore) -> str | None:
    signals = store.load_historical_signals(limit=1)
    if not signals:
        return None
    day = str(signals[0].get("market_date") or "")[:10]
    return day or None


def _normalize_learning_run(row: dict[str, Any]) -> dict[str, Any]:
    """Preserve legacy audit payloads while removing fake zero performance truth."""

    normalized = dict(row)
    truth = dict(normalized.get("truth_report") or {})
    if int(truth.get("sample_size") or 0) == 0:
        for key in (
            "average_return_pct",
            "median_return_pct",
            "win_rate_pct",
            "worst_day_return_pct",
            "best_day_return_pct",
            "max_drawdown_pct",
            "missing_outcome_rate_pct",
        ):
            truth[key] = None
        truth["evidence_status"] = "insufficient_real_outcomes"
        outlier = dict(truth.get("outlier") or {})
        outlier["outlier_dependency"] = None
        outlier["outlier_dependent"] = False
        truth["outlier"] = outlier
        for key in ("top1", "top3", "top5"):
            summary = dict(truth.get(key) or {})
            if int(summary.get("sample_size") or 0) == 0:
                for metric in (
                    "avg_return_pct",
                    "median_return_pct",
                    "win_rate_pct",
                    "max_drawdown_pct",
                ):
                    summary[metric] = None
            truth[key] = summary
    normalized["truth_report"] = truth
    return normalized


def alpha_doctor(
    *,
    config_path: str | Path = DEFAULT_WEB_CONFIG,
    out_dir: str | Path = "outputs/alpha_doctor",
) -> dict[str, Any]:
    result = web_source_doctor(config_path=config_path, out_dir=out_dir, print_rows=False)
    result["alphaops_checks"] = {
        "research_only": True,
        "order_execution": "disabled_by_design",
        "manual_fallback": "data/inbox/screener",
    }
    return result


def alpha_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    out_dir: str | Path = "outputs/alpha_report",
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path, read_only=True)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    labels = load_production_alpha_learning_labels(store)
    real_days = _real_days(labels)
    truth = build_truth_report(labels, real_days_collected=real_days)
    status = alpha_status(db_path=db_path)
    review_metrics = _alpha_daily_review_metrics(
        store.load_daily_review_runs(limit=5000),
        store.load_learning_backfeed_events(limit=50000),
    )
    summary = {
        "created_at": utc_now_iso(),
        "status": status,
        "truth_report": truth,
        "daily_review_metrics": review_metrics,
        "source_reliability": store.load_alpha_source_reliability(),
        "setup_memory": store.load_alpha_setup_memory(),
        "alpha_summary_message": format_alpha_summary({"truth_report": truth}),
    }
    _write_json(output_dir / "alpha_report.json", summary)
    _write_markdown(output_dir / "alpha_report.md", summary)
    return {**summary, "out_dir": str(output_dir)}


def _persist_run_contract(
    output_dir: Path,
    *,
    scan_id: str,
    generated_at: str,
    ranked_count: int,
    signals: list[dict[str, Any]],
    review: dict[str, Any],
    source_summary: dict[str, Any],
    enrichment_summary: dict[str, Any] | None,
    notification_stats: dict[str, Any],
    notification_channel: str,
    notification_dry_run: bool,
    notification_status_override: str = "",
    receipt_verifier: AuthenticatedStrategyReceiptResolver | None = None,
    artifact_name: str = "alpha_run_contract.json",
) -> AlphaRunContract:
    contract = build_alpha_run_contract(
        scan_id=scan_id,
        generated_at=generated_at,
        ranked_count=ranked_count,
        signals=signals,
        review=review,
        source_summary=source_summary,
        enrichment_summary=enrichment_summary,
        notification_stats=notification_stats,
        notification_channel=notification_channel,
        notification_dry_run=notification_dry_run,
        notification_status_override=notification_status_override,
        receipt_verifier=receipt_verifier,
    )
    _write_json(output_dir / artifact_name, contract.to_dict())
    return contract


def _load_frozen_luna_slate(path: Path, *, market_date: str) -> dict[str, Any]:
    """Reuse the first valid daily slate on retries; never replace its bytes."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError("Persisted Luna slate is unreadable") from exc
    if not isinstance(payload, dict):
        raise SnapshotValidationError("Persisted Luna slate is not an object")
    try:
        return validate_ranked_research_slate(payload, market_date=market_date)
    except ValueError as exc:
        raise SnapshotValidationError("Persisted Luna slate failed integrity checks") from exc


def _merge_lane_candidates(
    mover_candidates: list[dict[str, Any]],
    core_candidates: list[dict[str, Any]],
    *,
    lane_eligibility: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Merge already-produced lane candidates, preserving overlap metadata."""

    merged: dict[str, dict[str, Any]] = {}
    lane_rows = [("mover", row) for row in mover_candidates] + [
        ("core", row) for row in core_candidates
    ]
    for lane, source_row in lane_rows:
        row = dict(source_row)
        row.setdefault("universe_lane", lane)
        row.setdefault("evidence_lane", lane)
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        if ticker not in merged:
            merged[ticker] = dict(row)
            continue
        prior = merged[ticker]
        # Keep one independently collected row intact.  A safe row wins over
        # an unsafe overlap when lane admission is equal.  A fully admissible
        # row wins first; core is the deterministic tie-breaker only when both
        # row safety and lane admission are equal.
        prior_lane = str(prior.get("evidence_lane") or "mover").strip().lower()
        prior_admissible = row_research_admissible(prior)
        row_admissible = row_research_admissible(row)
        prior_lane_eligible = (
            lane_eligibility.get(prior_lane, False) if lane_eligibility else True
        )
        row_lane_eligible = (
            lane_eligibility.get(lane, False) if lane_eligibility else True
        )
        prior_rank = (
            prior_admissible and prior_lane_eligible,
            prior_admissible,
            prior_lane_eligible,
        )
        row_rank = (
            row_admissible and row_lane_eligible,
            row_admissible,
            row_lane_eligible,
        )
        choose_row = row_rank >= prior_rank
        current = dict(row) if choose_row else dict(prior)
        evidence_lane = lane if choose_row else prior_lane
        current["universe_lane"] = "mover+core"
        current["evidence_lane"] = evidence_lane
        merged[ticker] = current
    return sorted(
        merged.values(),
        key=lambda row: float(row.get("score") or row.get("total_score") or 0),
        reverse=True,
    )


def _alpha_scoring_cohort(
    all_candidates: list[dict[str, Any]], ranked: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return the full enriched cohort, retaining a legacy empty-cohort fallback.

    ``ranked`` is capped for presentation by ``ScannerConfig.top_n``.  Alpha
    scoring must see every row already produced by the mover/core enrichment
    steps so downstream safety and plan gates can refill the research slate.
    The fallback keeps callers that provide only a ranked slice compatible and
    never expands provider work.
    """

    source = all_candidates if all_candidates else ranked
    # ``ScanResult.all_candidates`` intentionally contains formula avoids for
    # analytics.  Keep those out of Alpha scoring as well: only the full
    # non-avoid cohort is a reserve, and the later publication gates remain
    # authoritative for current safety/plan eligibility.
    return [row for row in source if not _has_avoid_reason(row)]


def _has_avoid_reason(row: dict[str, Any]) -> bool:
    value = row.get("avoid_reasons")
    if isinstance(value, (list, tuple, set)):
        return any(str(item).strip() for item in value)
    return str(value or "").strip().lower() not in {"", "none", "false"}


def _alphaops_scanner_config(config: Any) -> Any:
    """Use a liquid day-trading universe instead of the legacy penny-gap profile."""

    return config.with_overrides(
        min_gap_pct=1.0,
        ideal_gap_low_pct=3.0,
        ideal_gap_high_pct=25.0,
        max_credible_gap_pct=50.0,
        min_premarket_dollar_volume=1_000_000.0,
        min_premarket_share_volume=50_000,
        min_price=1.0,
        max_price=500.0,
        top_n=20,
        wide_spread_pct=3.0,
        premarket_enrichment_max_candidates=60,
    )


def _parse_market_date(value: str) -> str:
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError("market_date must be an ISO date (YYYY-MM-DD)") from exc
    return parsed.date().isoformat()


def _verify_ranked_sec_safety(
    ranked: list[dict[str, Any]],
    *,
    source_config: Any,
    store: SQLiteScanStore,
    out_dir: Path,
    as_of: str,
    rehearsal_mode: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Verify SEC safety against the candidates that actually survived ranking."""

    tickers = [str(row.get("ticker") or "").upper() for row in ranked if row.get("ticker")]
    if not tickers:
        return ranked, {"status": "NO_RANKED_CANDIDATES", "checked_tickers": []}
    if rehearsal_mode:
        return ranked, {
            "status": "REHEARSAL_REUSED_COLLECTION_EVIDENCE",
            "checked_tickers": sorted(
                ticker
                for ticker, row in zip(tickers, ranked, strict=False)
                if str(row.get("sec_risk_status") or "").upper() in {"CLEAR", "BLOCKED"}
            ),
        }
    source = get_source(source_config, "sec_edgar")
    if source is None:
        return ranked, {
            "status": "DISABLED",
            "checked_tickers": [],
            "unchecked_tickers": sorted(set(tickers)),
        }
    summary = collect_sec_risk(
        source=source,
        config=source_config,
        tickers=tickers,
        out_dir=out_dir,
        store=store,
        persist=True,
    )
    enriched = enrich_rows_with_sec_risk(
        ranked,
        list(summary.get("events") or []),
        checked_tickers=list(summary.get("checked_tickers") or []),
        as_of=as_of,
    )
    return enriched, {
        "status": str(summary.get("status") or "partial").upper(),
        "checked_tickers": list(summary.get("checked_tickers") or []),
        "unchecked_tickers": list(summary.get("unchecked_tickers") or []),
        "event_count": int(summary.get("event_count") or 0),
        "candidate_count": len(tickers),
        "verified_count": len(set(summary.get("checked_tickers") or [])),
        "unverified_count": len(set(summary.get("unchecked_tickers") or [])),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _merge_ranked_safety(
    candidates: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    safety_by_ticker = {
        str(row.get("ticker") or "").upper(): row for row in ranked if row.get("ticker")
    }
    fields = (
        "sec_risk_status",
        "corporate_action_status",
        "recent_offering",
        "reverse_split_90d",
        "sec_active_risk_events",
        "coverage_warning",
    )
    merged: list[dict[str, Any]] = []
    for candidate in candidates:
        updated = dict(candidate)
        safety = safety_by_ticker.get(str(updated.get("ticker") or "").upper())
        if safety:
            for field in fields:
                if field in safety:
                    updated[field] = safety[field]
        merged.append(updated)
    return merged


def _register_alpaca_screening_universe(
    store: SQLiteScanStore,
    *,
    source_summary: dict[str, Any],
    market_date: str,
) -> dict[str, Any]:
    attempts = list(source_summary.get("attempts") or [])
    evidence = next(
        (
            dict(row.get("universe_evidence") or {})
            for row in attempts
            if str(row.get("source_type") or "") == "alpaca_screener_api"
            and str(row.get("status") or "") == "success"
            and isinstance(row.get("universe_evidence"), dict)
        ),
        {},
    )
    members = list(evidence.get("members") or [])
    if not members:
        return {
            "status": "UNAVAILABLE",
            "reason": "authenticated_alpaca_screening_universe_missing",
            "member_count": 0,
        }
    if evidence.get("registration_approved") is not True:
        return {
            "status": "BLOCKED_CONFIGURATION",
            "reason": "alpaca_v6_registration_not_approved",
            "member_count": len(members),
        }
    contract = {
        "provider_id": "alpaca",
        "dataset_id": str(evidence.get("dataset_id") or "stocks-screener-plus-active-assets"),
        "dataset_version": str(evidence.get("dataset_version") or evidence.get("retrieved_at")),
        "terms_reference": str(evidence.get("terms_reference") or "https://docs.alpaca.markets/"),
        "entitlement_reference": str(
            evidence.get("entitlement_reference") or "configured-alpaca-account"
        ),
        "accountable_contact": str(
            evidence.get("accountable_contact") or "dawnstrikebot@gmail.com"
        ),
        "approval_status": "APPROVED",
        "critical_truth_complete": True,
        "registration_allowed": True,
    }
    contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True).encode("utf-8")).hexdigest()
    raw_hash = str(evidence.get("raw_artifact_sha256") or "")
    if len(raw_hash) != 64:
        return {
            "status": "BLOCKED_EVIDENCE",
            "reason": "alpaca_universe_artifact_hash_missing",
            "member_count": len(members),
        }
    lineage = {
        "source_id": "alpaca:stocks-screener-plus-active-assets",
        **contract,
        "source_contract_hash_sha256": contract_hash,
        "retrieved_at": str(evidence.get("retrieved_at") or utc_now_iso()),
        "raw_artifact_sha256": raw_hash,
        "configuration_hash_sha256": hashlib.sha256(
            json.dumps(
                {
                    "market_date": market_date,
                    "policy": ALPHAOPS_RADAR_VERSION,
                    "members": sorted(str(row.get("ticker") or "") for row in members),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    }
    registered = register_alpha_v6_universe(
        store,
        as_of_date=market_date,
        members=members,
        source_lineage=lineage,
    )
    return {
        "status": "REGISTERED",
        "universe_id": registered.get("universe_id"),
        "member_count": int(registered.get("membership_count") or len(members)),
        "persisted": registered.get("persisted"),
        "source_lineage_hash_sha256": registered.get("source_lineage_hash_sha256"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _research_radar(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select safe-to-study conditional setups even when official evidence is incomplete."""

    rows: list[dict[str, Any]] = []
    for signal in signals:
        hard = signal.get("hard_avoid_reasons")
        hard_reasons = (
            [str(item) for item in hard]
            if isinstance(hard, list)
            else [part for part in str(hard or "").split(";") if part]
        )
        if hard_reasons:
            continue
        if bool(signal.get("stale_data_flag")):
            continue
        if (_number(signal.get("source_confidence")) or 0.0) < 25.0:
            continue
        if str(signal.get("source_quality_status") or "").upper() not in {
            "VERIFIED",
            "LIMITED",
        }:
            continue
        if str(signal.get("halt_status") or "").upper() != "CLEAR":
            continue
        if str(signal.get("sec_risk_status") or "").upper() != "CLEAR":
            continue
        if str(signal.get("corporate_action_status") or "").upper() != "CLEAR":
            continue
        risk_text = ";".join(
            str(signal.get(field) or "")
            for field in ("risk_flags", "coverage_warning", "catalyst_risk_flags")
        ).lower()
        if "wide_spread" in risk_text or "extreme spread" in risk_text:
            continue
        trigger = _number(signal.get("entry_trigger") or signal.get("breakout_trigger"))
        stop = _number(signal.get("invalidation") or signal.get("invalidation_level"))
        dollar_volume = _number(signal.get("dollar_volume")) or 0.0
        spread = _number(signal.get("spread_pct")) or 0.0
        gap = _number(signal.get("gap_pct"))
        if (
            trigger is None
            or stop is None
            or not (trigger > stop > 0)
            or dollar_volume < 1_000_000
            or spread > 3.0
            or gap is None
            or not (1.0 <= gap <= 50.0)
        ):
            continue
        stop_distance_pct = (trigger - stop) / trigger * 100.0
        if stop_distance_pct > 8.0:
            continue
        target_options = (
            (
                "first_range_extension",
                _number(signal.get("target_1") or signal.get("first_target")),
            ),
            (
                "stretch_range_extension",
                _number(signal.get("target_2") or signal.get("stretch_target")),
            ),
        )
        chosen_target = None
        target_role = ""
        reward_risk = 0.0
        for role, target in target_options:
            if target is None or target <= trigger:
                continue
            candidate_rr = (target - trigger) / (trigger - stop)
            if candidate_rr >= 1.5:
                chosen_target = target
                target_role = role
                reward_risk = candidate_rr
                break
        if chosen_target is None:
            continue
        reasons = signal.get("alert_gate_reasons") or []
        reason_text = (
            "; ".join(str(item) for item in reasons) if isinstance(reasons, list) else str(reasons)
        )
        rows.append(
            {
                **signal,
                "cohort": ALPHAOPS_RADAR_COHORT,
                "strategy_version": ALPHAOPS_RADAR_VERSION,
                "decision_tier": "research_radar",
                "classification": "RESEARCH RADAR",
                "review_label": "CONDITIONAL PAPER WATCH",
                "reward_risk_ratio": round(reward_risk, 3),
                "radar_target": round(chosen_target, 4),
                "radar_target_role": target_role,
                "radar_stop_distance_pct": round(stop_distance_pct, 3),
                "radar_reason": reason_text or "official evidence gate incomplete",
                "research_only": True,
                "broker_execution_enabled": False,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            float(row.get("alpha_score") or 0.0),
            float(row.get("dollar_volume") or 0.0),
        ),
        reverse=True,
    )[:3]


def _annotate_research_radar(
    signals: list[dict[str, Any]],
    radar: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    radar_by_id = {str(row.get("signal_id") or row.get("signal_key") or ""): row for row in radar}
    output: list[dict[str, Any]] = []
    for signal in signals:
        signal_id = str(signal.get("signal_id") or signal.get("signal_key") or "")
        selected = radar_by_id.get(signal_id)
        if not selected:
            output.append(signal)
            continue
        output.append(
            {
                **signal,
                "research_radar_selected": True,
                "research_radar_target": selected.get("radar_target"),
                "research_radar_target_role": selected.get("radar_target_role"),
                "research_radar_reward_risk_ratio": selected.get("reward_risk_ratio"),
                "research_radar_stop_distance_pct": selected.get("radar_stop_distance_pct"),
                "research_radar_policy": ALPHAOPS_RADAR_VERSION,
            }
        )
    return output


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _signal_payload(row: dict[str, Any], scan_id: str, timestamp: str, rank: int) -> dict[str, Any]:
    strategy_id, strategy_version = alphaops_strategy_contract(timestamp)
    payload = {
        **row,
        "strategy_id": str(row.get("strategy_id") or strategy_id),
        "strategy_version": str(row.get("strategy_version") or strategy_version),
        "scan_id": scan_id,
        "market_date": str(row.get("market_date") or timestamp[:10]),
        "rank": rank,
        "timestamp": timestamp,
        "signal_key": f"{scan_id}:{rank}:{row.get('ticker')}",
        "telegram_key": f"alpha:{scan_id}:{rank}:{row.get('ticker')}",
        "alert_sent": False,
    }
    if payload["strategy_id"] == "alphaops_v5":
        payload["session_id"] = str(payload.get("session_id") or "regular")
        payload["entry_window"] = str(payload.get("entry_window") or "09:30-15:30")
        plan = construct_alphaops_v5_plan(payload, decision_at=timestamp)
        payload["alphaops_market_structure_plan"] = plan.to_dict()
        payload["plan_hash_sha256"] = plan.plan_hash_sha256
        payload["market_structure_plan"] = plan.status == COMPLETE
        payload["entry_observation_provenance"] = plan.status == COMPLETE
        payload["stop_observation_provenance"] = plan.status == COMPLETE
        payload["target_observation_provenance"] = plan.status == COMPLETE
        payload["plan_levels_frozen"] = plan.status == COMPLETE
        payload["plan_construction_status"] = (
            "LEGACY_RESEARCH_BASELINE"
            if (
                plan.status == NO_VALID_PLAN
                and payload.get("legacy_plan_status") == "LEGACY_RESEARCH_BASELINE"
            )
            else plan.status
        )
        payload["plan_construction_reason"] = (
            str(payload.get("legacy_plan_reason") or "")
            if (
                plan.status == NO_VALID_PLAN
                and payload.get("legacy_plan_status") == "LEGACY_RESEARCH_BASELINE"
            )
            else plan.reason
        )
        if plan.status == COMPLETE:
            # These are the already-frozen values, carried forward unchanged
            # for downstream receipts and alert gates.
            payload["direction"] = plan.direction
            payload["entry_watch_level"] = plan.entry
            payload["breakout_trigger"] = plan.entry
            payload["invalidation_level"] = plan.stop
            payload["invalidation"] = plan.stop
            payload["target_1"] = plan.target
            payload["first_target"] = plan.target
            payload["target_basis_kind"] = plan.target_basis_kind
            payload["target_derived_from_risk"] = False
    return payload


def _merge_strategy_adapter_signals(
    signals: list[dict[str, Any]], adapter_rows: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Dedupe one Morning row per ticker while retaining all contributors.

    Adapter rows are intentionally receipt-built before this function runs.
    The selected row therefore carries the exact canonical receipt for the
    primary strategy plus a lossless contributor list/receipt projection for
    every other enabled strategy that independently qualified the same ticker.
    """

    source = [
        dict(row)
        for row in [*(signals or []), *(adapter_rows or [])]
        if isinstance(row, dict)
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for row in source:
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        if not ticker:
            passthrough.append(row)
            continue
        grouped.setdefault(ticker, []).append(row)

    def score_value(row: dict[str, Any]) -> float:
        try:
            return float(row.get("alpha_score") or row.get("score") or float("-inf"))
        except (TypeError, ValueError):
            return float("-inf")

    def primary_bucket(row: dict[str, Any]) -> int:
        adapter = bool(str(row.get("strategy_adapter") or "").strip())
        eligible = row.get("research_pick_eligible") is True
        if eligible and not adapter:
            return 0
        if eligible and adapter:
            return 1
        if not adapter:
            return 2
        return 3

    output: list[dict[str, Any]] = []
    for rows in grouped.values():
        ordered = sorted(
            rows,
            key=lambda row: (
                primary_bucket(row),
                -score_value(row),
                str(row.get("strategy_id") or ""),
                str(row.get("signal_id") or row.get("signal_key") or ""),
            ),
        )
        primary = dict(ordered[0])
        contributors: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        seen_contributors: set[tuple[str, str, str, str]] = set()
        seen_receipts: set[str] = set()

        def add_contributor(
            row: dict[str, Any],
            *,
            _seen_contributors: set[tuple[str, str, str, str]] = seen_contributors,
            _seen_receipts: set[str] = seen_receipts,
            _contributors: list[dict[str, Any]] = contributors,
            _receipts: list[dict[str, Any]] = receipts,
        ) -> None:
            strategy_id = str(
                row.get("strategy_id") or row.get("decision_strategy_id") or ""
            ).strip()
            strategy_version = str(row.get("strategy_version") or "").strip()
            strategy_fp = str(row.get("strategy_semantics_fingerprint") or "").strip()
            source_id = str(
                row.get("source_signal_id")
                or row.get("prior_session_signal_id")
                or row.get("signal_id")
                or row.get("signal_key")
                or ""
            ).strip()
            key = (strategy_id, strategy_version, strategy_fp, source_id)
            if not strategy_id or key in _seen_contributors:
                return
            _seen_contributors.add(key)
            receipt = row.get("strategy_decision_receipt") or row.get("decision_receipt")
            receipt_id = str(row.get("receipt_id") or "").strip()
            if isinstance(receipt, dict):
                receipt_id = str(receipt.get("receipt_id") or receipt_id).strip()
                if receipt_id and receipt_id not in _seen_receipts:
                    _receipts.append(dict(receipt))
                    _seen_receipts.add(receipt_id)
            _contributors.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "strategy_semantics_fingerprint": strategy_fp,
                    "source_signal_id": source_id,
                    "signal_id": str(row.get("signal_id") or ""),
                    "strategy_adapter": str(row.get("strategy_adapter") or ""),
                    "receipt_id": receipt_id,
                    "receipt_hash_sha256": str(row.get("receipt_hash_sha256") or ""),
                    "receipt_status": str(
                        row.get("strategy_receipt_construction_status") or "MISSING"
                    ),
                    "research_pick_eligible": row.get("research_pick_eligible"),
                    "paper_entry_eligible": row.get("paper_entry_eligible"),
                    "decision_receipt": dict(receipt) if isinstance(receipt, dict) else None,
                }
            )

        # Existing nested contributors are carried first, then every source
        # row.  This makes the helper idempotent on a retry/review path.
        for existing in primary.get("strategy_contributors") or []:
            if isinstance(existing, dict):
                add_contributor(existing)
        for row in ordered:
            add_contributor(row)
            for existing in row.get("strategy_contributors") or []:
                if isinstance(existing, dict):
                    add_contributor(existing)
        primary["strategy_contributors"] = contributors
        primary["strategy_contributor_count"] = len(contributors)
        primary["strategy_contributor_ids"] = sorted(
            {
                str(item["strategy_id"])
                for item in contributors
                if str(item.get("strategy_id") or "")
            }
        )
        primary["strategy_decision_receipts"] = receipts
        primary["strategy_contribution_status"] = (
            "COMPLETE"
            if contributors and all(item.get("receipt_id") for item in contributors)
            else "DISCLOSED_GAPS"
        )
        primary["research_only"] = True
        primary["broker_execution"] = "disabled"
        primary["broker_execution_enabled"] = False
        output.append(primary)
    output.extend(passthrough)
    return output


def _strategy_adapter_contributor_count(signals: list[dict[str, Any]]) -> int:
    accepted_labels = {LEGACY_SOURCE_LABEL, GOVERNED_SOURCE_LABEL}
    return sum(
        1
        for row in signals
        for contributor in row.get("strategy_contributors") or []
        if str(contributor.get("strategy_adapter") or "").strip() in accepted_labels
    )


def _attach_authenticated_alpaca_structure(
    row: dict[str, Any], *, decision_at: str
) -> dict[str, Any]:
    """Bind v5 legs to completed Alpaca observations at the production seam.

    The scorer only ranks candidates.  Immediately after scoring, this seam
    turns the authenticated premarket observation into three explicit legs;
    absent/stale/fallback evidence produces no observations and therefore the
    constructor emits ``NO_VALID_PLAN``.  No Yahoo value, range extension, or
    reward/risk search is allowed through this path.
    """

    output = dict(row)
    strategy_id = str(output.get("strategy_id") or "")
    expected_strategy_id, _ = alphaops_strategy_contract(decision_at)
    primary = str(output.get("enrichment_primary_source") or "").lower()
    source = str(
        output.get("enrichment_range_source")
        or output.get("premarket_range_source")
        or ""
    ).lower()
    observed = str(output.get("enrichment_observed_at") or "").strip()
    completed = str(output.get("enrichment_bar_completed_at") or "").strip()
    source_hash = str(output.get("enrichment_observation_sha256") or "").strip().lower()
    observation_payload_json = str(
        output.get("enrichment_observation_payload_json") or ""
    ).strip()
    observation_payload: dict[str, Any] | None = None
    observation_payload_hash_ok = False
    if observation_payload_json:
        try:
            parsed_payload = json.loads(observation_payload_json)
            if isinstance(parsed_payload, dict):
                canonical_payload = json.dumps(
                    parsed_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                observation_payload_hash_ok = _valid_sha256(source_hash) and hashlib.sha256(
                    canonical_payload.encode("utf-8")
                ).hexdigest() == source_hash
                if observation_payload_hash_ok:
                    observation_payload = parsed_payload
        except (TypeError, ValueError, json.JSONDecodeError):
            observation_payload = None
    ticker = str(output.get("ticker") or output.get("symbol") or "").upper()
    high = _number(output.get("premarket_high"))
    low = _number(output.get("premarket_low"))
    premarket_raw_json = str(output.get("premarket_raw_payload_json") or "").strip()
    premarket_hash = str(output.get("premarket_source_hash_sha256") or "").strip().lower()
    premarket_reconciles = False
    if premarket_raw_json:
        try:
            raw_premarket = json.loads(premarket_raw_json)
            canonical_premarket = json.dumps(
                raw_premarket,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            raw_bars = raw_premarket.get("bars") if isinstance(raw_premarket, dict) else None
            requested_dt = _parse_structure_time(
                raw_premarket.get("requested_at") if isinstance(raw_premarket, dict) else None
            )
            decision_dt = _parse_structure_time(decision_at)
            bar_times = [
                _parse_structure_time(item.get("timestamp"))
                for item in raw_bars or []
                if isinstance(item, dict)
            ]
            valid_bar_times = all(item is not None for item in bar_times)
            ordered_bar_times = bool(bar_times) and all(
                left < right for left, right in zip(bar_times, bar_times[1:], strict=False)
            )
            session_date = decision_dt.astimezone(EASTERN).date() if decision_dt else None
            session_valid = bool(session_date) and all(
                item is not None
                and item.astimezone(EASTERN).date() == session_date
                and (item.astimezone(EASTERN).hour, item.astimezone(EASTERN).minute)
                >= (4, 0)
                and (item.astimezone(EASTERN).hour, item.astimezone(EASTERN).minute)
                < (9, 30)
                for item in bar_times
            )
            complete_by_request = bool(requested_dt) and all(
                item is not None and item + timedelta(minutes=1) <= requested_dt
                for item in bar_times
            )
            latest_matches_aggregate = bool(bar_times) and (
                bar_times[-1] == _parse_structure_time(observed)
                and bar_times[-1] + timedelta(minutes=1)
                == _parse_structure_time(completed)
            )
            high_values = [
                _number(item.get("high"))
                for item in raw_bars or []
                if isinstance(item, dict)
            ]
            low_values = [
                _number(item.get("low"))
                for item in raw_bars or []
                if isinstance(item, dict)
            ]
            premarket_reconciles = (
                _valid_sha256(premarket_hash)
                and hashlib.sha256(canonical_premarket.encode("utf-8")).hexdigest()
                == premarket_hash
                and isinstance(raw_premarket, dict)
                and str(raw_premarket.get("ticker") or "").upper() == str(
                    output.get("ticker") or output.get("symbol") or ""
                ).upper()
                and str(raw_premarket.get("feed") or "").lower()
                == source.rsplit("_", 1)[-1]
                and requested_dt is not None
                and decision_dt is not None
                and requested_dt == decision_dt
                and isinstance(raw_bars, list)
                and raw_bars
                and valid_bar_times
                and ordered_bar_times
                and session_valid
                and complete_by_request
                and latest_matches_aggregate
                and all(
                    str(item.get("ticker") or "").upper() == ticker
                    for item in raw_bars
                    if isinstance(item, dict)
                )
                and high_values
                and low_values
                and all(value is not None and value > 0 for value in high_values + low_values)
                and high == max(high_values)
                and low == min(low_values)
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            premarket_reconciles = False
    prior_high = _number(output.get("prior_daily_high"))
    prior_observed = str(output.get("prior_daily_high_observed_at") or "").strip()
    prior_completed = str(output.get("prior_daily_high_completed_at") or "").strip()
    prior_completion_semantics = str(
        output.get("prior_daily_high_completion_semantics") or ""
    ).strip()
    prior_source = str(output.get("prior_daily_high_source") or "").strip()
    prior_url = str(output.get("prior_daily_high_source_url") or "").strip()
    prior_hash = str(output.get("prior_daily_high_source_hash") or "").strip().lower()
    prior_raw_json = str(output.get("prior_daily_high_raw_payload_json") or "").strip()
    prior_raw_hash_ok = False
    prior_raw_reconciles = False
    if prior_raw_json:
        try:
            parsed_prior_raw = json.loads(prior_raw_json)
            canonical_prior_raw = json.dumps(
                parsed_prior_raw,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            prior_raw_hash_ok = _valid_sha256(prior_hash) and hashlib.sha256(
                canonical_prior_raw.encode("utf-8")
            ).hexdigest() == prior_hash
            raw_bar = (
                parsed_prior_raw.get("bar")
                if isinstance(parsed_prior_raw, dict)
                else None
            )
            raw_bar_high = (
                _number(raw_bar.get("h")) if isinstance(raw_bar, dict) else None
            )
            raw_high = (
                _number(parsed_prior_raw.get("high"))
                if isinstance(parsed_prior_raw, dict)
                else None
            )
            prior_raw_reconciles = (
                isinstance(parsed_prior_raw, dict)
                and str(parsed_prior_raw.get("ticker") or "").upper()
                == str(output.get("ticker") or output.get("symbol") or "").upper()
                and str(parsed_prior_raw.get("timestamp") or "") == prior_observed
                and raw_high is not None
                and abs(raw_high - prior_high) <= 1e-9
                and raw_bar_high is not None
                and abs(raw_bar_high - prior_high) <= 1e-9
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            prior_raw_hash_ok = False
    authenticated = (
        (strategy_id or expected_strategy_id) == "alphaops_v5"
        and primary.startswith("alpaca_market_data_")
        and source.startswith("alpaca_market_data_")
        and not bool(output.get("enrichment_was_fallback"))
        and str(output.get("enrichment_status") or "").lower() == "verified"
        and output.get("enrichment_is_complete") is True
        and observed
        and completed
        and _valid_sha256(source_hash)
        and prior_high is not None
        and prior_high > 0
        and prior_observed
        and prior_completed
        and prior_completion_semantics == "availability_boundary"
        and prior_source.startswith("alpaca_market_data_")
        and prior_source == source
        and prior_url
        and _valid_sha256(prior_hash)
        and prior_raw_hash_ok
        and prior_raw_reconciles
        and premarket_reconciles
    )
    if not authenticated:
        # Deliberately erase any legacy/range-derived plan inputs in a
        # production AlphaOps v5 row.  Test/fixture callers that already carry
        # explicit source-bound observations remain supported by _signal_payload.
        if strategy_id == "alphaops_v5" and primary:
            output.pop("market_structure_observations", None)
            output.pop("target_observations", None)
        return output
    payload_ticker = str((observation_payload or {}).get("ticker") or "").upper()
    payload_high = _number((observation_payload or {}).get("premarket_high"))
    payload_low = _number((observation_payload or {}).get("premarket_low"))
    payload_source = str((observation_payload or {}).get("source") or "")
    payload_observed = str((observation_payload or {}).get("observed_at") or "")
    payload_completed = str((observation_payload or {}).get("bar_completed_at") or "")
    payload_is_complete = (observation_payload or {}).get("is_complete") is True
    premarket_receipt_matches = (
        observation_payload_hash_ok
        and payload_ticker == ticker
        and payload_high is not None
        and payload_low is not None
        and high is not None
        and low is not None
        and abs(payload_high - high) <= 1e-9
        and abs(payload_low - low) <= 1e-9
        and payload_source == source
        and payload_observed == observed
        and payload_completed == completed
        and payload_is_complete
    )
    if (
        high is None
        or low is None
        or high <= low
        or high <= 0
        or low <= 0
        or not premarket_receipt_matches
    ):
        return output
    output["market_structure_observations"] = {
        "entry": _strict_structure_observation(
            ticker=ticker, role="entry", value=high, observed_at=observed,
            completed_at=completed,
            source=source,
            source_url=str(output.get("premarket_range_source_url") or ""),
            source_hash=premarket_hash, observation_kind="premarket_high",
        ),
        "stop": _strict_structure_observation(
            ticker=ticker, role="stop", value=low, observed_at=observed,
            completed_at=completed,
            source=source,
            source_url=str(output.get("premarket_range_source_url") or ""),
            source_hash=premarket_hash, observation_kind="premarket_low",
        ),
        "target": _strict_structure_observation(
            ticker=ticker, role="target", value=prior_high, observed_at=prior_observed,
            completed_at=prior_completed, source=prior_source, source_url=prior_url,
            source_hash=prior_hash, observation_kind="prior_day_resistance",
            completion_semantics=prior_completion_semantics,
        ),
    }
    output["entry_watch_level"] = high
    output["invalidation_level"] = low
    output["target_1"] = prior_high
    output["target_basis_kind"] = "prior_day_resistance"
    output["target_derived_from_risk"] = False
    return output


def _strict_structure_observation(
    *, ticker: str, role: str, value: float, observed_at: str,
    completed_at: str, source: str, source_url: str, source_hash: str,
    observation_kind: str, completion_semantics: str = "bar_completion",
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "role": role,
        "value": value,
        "raw_value": value,
        "observed_at": observed_at,
        "completed_at": completed_at,
        "completion_semantics": completion_semantics,
        "source": source,
        "source_url": source_url,
        "source_hash": source_hash,
        "observation_kind": observation_kind,
        "derivation_policy": "identity",
        "is_complete": True,
    }


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _parse_structure_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _apply_strategy_decision_receipts(
    signals: list[dict[str, Any]],
    *,
    store: SQLiteScanStore,
    config: Any,
    decision_at: str,
    source_summary: dict[str, Any],
    gap_resolver: Any | None = None,
) -> dict[str, Any]:
    """Build, resolve, re-evaluate, and persist receipts before final gates."""

    if not getattr(config, "strategy_evidence_enabled", False):
        return {"status": "disabled", "computed": 0, "persisted": 0, "reused": 0}

    from intraday_scanner.decisioning.condition_registry import (
        registry_for_strategy,
        strategy_ids,
    )
    from intraday_scanner.decisioning.contracts import ConditionCategory, ConditionStatus
    from intraday_scanner.decisioning.evidence_resolver import CONDITION_CLAIM_TYPES

    code_sha = str(os.environ.get("DAWNSTRIKE_CODE_SHA") or "").strip()
    shadow_only = bool(getattr(config, "strategy_evidence_shadow_only", True))
    max_candidates = max(1, int(getattr(config, "strategy_evidence_max_candidates", 1)))
    max_symbols = max(
        1,
        int(getattr(config, "indeterminate_research_max_symbols", max_candidates)),
    )
    resolution_budget = min(max_candidates, max_symbols)
    timeout_seconds = float(getattr(config, "indeterminate_research_timeout_seconds", 60.0))
    max_tool_calls = int(getattr(config, "indeterminate_research_max_tool_calls", 3))
    supported = set(strategy_ids())
    eligible_rows: list[tuple[int, dict[str, Any]]] = []
    uncovered: list[dict[str, Any]] = []
    construction_records: dict[int, dict[str, Any]] = {}

    def _mark_construction_failure(index: int, row: dict[str, Any], reason: str) -> None:
        record = {
            "status": "FAIL_CLOSED",
            "rank": row.get("rank") or index + 1,
            "ticker": str(row.get("ticker") or "").upper(),
            "strategy_id": str(row.get("strategy_id") or row.get("decision_strategy_id") or ""),
            "decision_at": decision_at,
            "reason": reason,
        }
        construction_records[index] = record
        row.update(
            {
                "strategy_receipt_construction_status": "FAIL_CLOSED",
                "strategy_receipt_construction_record": record,
                "strategy_receipt_gap": reason,
            }
        )

    for index, row in enumerate(signals):
        row.update(
            {
                "strategy_receipt_enabled": True,
                "strategy_receipt_shadow_only": shadow_only,
                "strategy_receipt_legacy_can_alert": bool(row.get("can_alert")),
                "strategy_receipt_construction_status": "PENDING",
                "strategy_receipt_disagreement": [],
            }
        )
        strategy_id = str(row.get("strategy_id") or row.get("decision_strategy_id") or "").strip()
        if not strategy_id:
            reason = "missing strategy identity"
        elif strategy_id not in supported:
            reason = f"strategy is outside universal registry: {strategy_id}"
        else:
            if not row.get("strategy_id"):
                row["strategy_id"] = strategy_id
            eligible_rows.append((index, row))
            continue
        row["strategy_receipt_gap"] = reason
        uncovered.append(
            {
                "rank": row.get("rank") or index + 1,
                "ticker": str(row.get("ticker") or "").upper(),
                "strategy_id": strategy_id or None,
                "reason": reason,
            }
        )
        _mark_construction_failure(index, row, reason)

    source_identity = str(source_summary.get("source_identity") or "").strip()
    if not source_identity:
        source_run_id = str(source_summary.get("run_id") or "").strip()
        if source_run_id:
            source_identity = f"web_auto_collect:{source_run_id}"

    if not code_sha:
        missing_identity_reason = "missing code identity"
    elif not source_identity:
        missing_identity_reason = "missing source identity"
    else:
        missing_identity_reason = ""

    service: StrategyDecisionService | None = None
    initial_records: list[tuple[int, dict[str, Any], Any]] = []
    if not missing_identity_reason:
        service = StrategyDecisionService(
            code_sha=code_sha,
            source_identity=source_identity,
            score_threshold=float(getattr(config, "alert_score_threshold", 0.0)),
        )
        individually_built: list[tuple[int, dict[str, Any], Any]] = []
        for index, row in eligible_rows:
            try:
                receipt = service.build_receipt(row, decision_at=decision_at)
            except (KeyError, TypeError, ValueError) as exc:
                _mark_construction_failure(
                    index, row, f"receipt construction failed: {type(exc).__name__}"
                )
                continue
            individually_built.append((index, row, receipt))
        if individually_built:
            try:
                batch = service.evaluate_candidates(
                    [row for _index, row, _receipt in individually_built],
                    decision_at=decision_at,
                )
                initial_records = [
                    (item[0], item[1], receipt)
                    for item, receipt in zip(individually_built, batch, strict=True)
                ]
            except (KeyError, TypeError, ValueError):
                initial_records = individually_built
    else:
        for index, row in eligible_rows:
            _mark_construction_failure(index, row, missing_identity_reason)

    unresolved_statuses = {
        ConditionStatus.MISSING_DISCLOSED,
        ConditionStatus.STALE,
        ConditionStatus.CONFLICT,
    }
    resolvable_categories = {
        ConditionCategory.AI_RESOLVABLE,
        ConditionCategory.EXECUTION_ONLY,
        ConditionCategory.ADVISORY,
    }

    def _record_rank(item: tuple[int, dict[str, Any], Any]) -> tuple[int, str, str, int]:
        index, row, _receipt = item
        try:
            rank = int(float(row.get("rank") or index + 1))
        except (TypeError, ValueError):
            rank = index + 1
        return (
            rank,
            str(row.get("ticker") or "").upper(),
            str(row.get("strategy_id") or ""),
            index,
        )

    ranked_initial_records = sorted(initial_records, key=_record_rank)
    selected_initial_records = ranked_initial_records[:max_candidates]
    selected_indices = {item[0] for item in selected_initial_records}
    deferred_count = max(0, len(ranked_initial_records) - len(selected_initial_records))
    resolution_candidates: list[tuple[int, dict[str, Any], Any, list[str]]] = []
    resolution_overrides: dict[int, dict[str, Any]] = {}
    resolution_bundles: dict[int, dict[str, Any]] = {}
    resolution_metrics: dict[str, Any] = {
        "request_count": 0,
        "web_search_call_count": 0,
        "token_usage": {},
        "cache_hits": 0,
        "elapsed_ms": 0,
        "attempts": 0,
    }
    for index, row, receipt in initial_records:
        specs = {spec.condition_id: spec for spec in registry_for_strategy(receipt.strategy_id)}
        result_by_id = {result.condition_id: result for result in receipt.condition_results}
        unresolved: list[str] = []
        resolver_condition_ids: list[str] = []
        for spec in specs.values():
            result = result_by_id[spec.condition_id]
            if (
                result.status not in unresolved_statuses
                or spec.category not in resolvable_categories
            ):
                continue
            unresolved.append(spec.condition_id)
            if (
                spec.resolver_id == "strategy_gap_resolver"
                and spec.condition_id in CONDITION_CLAIM_TYPES
            ):
                resolver_condition_ids.append(spec.condition_id)
        row["strategy_receipt_unresolved_conditions"] = unresolved
        row["strategy_receipt_resolution_candidates"] = resolver_condition_ids
        row["strategy_evidence_resolution_status"] = (
            "not_required" if not unresolved else "not_resolver_supported"
        )
        blocking_only_contextual = True
        for failure in receipt.all_blocking_failures:
            failure_spec = specs.get(failure)
            result = result_by_id.get(failure)
            if (
                failure_spec is None
                or failure_spec.category not in resolvable_categories
                or (result is not None and result.status == ConditionStatus.FAIL)
            ):
                blocking_only_contextual = False
                break
        if index in selected_indices and resolver_condition_ids and blocking_only_contextual:
            resolution_candidates.append((index, row, receipt, resolver_condition_ids))

    def _rank(item: tuple[int, dict[str, Any], Any, list[str]]) -> tuple[int, str, str, int]:
        index, row, _receipt, _condition_ids = item
        try:
            rank = int(float(row.get("rank") or index + 1))
        except (TypeError, ValueError):
            rank = index + 1
        return (
            rank,
            str(row.get("ticker") or "").upper(),
            str(row.get("strategy_id") or ""),
            index,
        )

    resolution_candidates.sort(key=_rank)
    selected_resolution_candidates = resolution_candidates[:resolution_budget]
    resolution_deferred_count = max(
        0, len(resolution_candidates) - len(selected_resolution_candidates)
    )
    resolution_metrics["selected_candidates"] = len(selected_initial_records)
    resolution_metrics["resolver_candidates"] = len(selected_resolution_candidates)
    if selected_resolution_candidates:
        resolver = gap_resolver or StrategyGapResolver(
            api_key=str(getattr(config, "openai_api_key", "") or ""),
            model=str(getattr(config, "scenario_openai_model", "") or ""),
            timeout_seconds=timeout_seconds,
            max_tool_calls=max_tool_calls,
            max_symbols=max_symbols,
        )
        deadline = time.monotonic() + (timeout_seconds * len(selected_resolution_candidates))
        for index, row, _receipt, condition_ids in selected_resolution_candidates:
            if time.monotonic() >= deadline:
                row["strategy_evidence_resolution_status"] = "time_budget_exhausted"
                continue
            resolution_metrics["attempts"] += 1
            try:
                result = resolver.resolve(
                    symbol=str(row.get("ticker") or row.get("symbol") or "").upper(),
                    market_date=str(row.get("market_date") or decision_at[:10]),
                    decision_at=decision_at,
                    condition_ids=condition_ids,
                    source_identity=source_identity,
                )
            except Exception as exc:  # provider failures remain disclosed gaps
                row["strategy_evidence_resolution_status"] = (
                    f"provider_failure:{type(exc).__name__}"
                )
                continue
            row["strategy_evidence_resolution_status"] = str(
                result.get("status") or "provider_failure"
            )
            overrides: dict[str, Any] = {}
            for raw_condition in result.get("condition_results") or ():
                if not isinstance(raw_condition, dict):
                    continue
                condition_id = str(raw_condition.get("condition_id") or "")
                if condition_id in condition_ids:
                    overrides[condition_id] = dict(raw_condition)
            if overrides:
                resolution_overrides[index] = overrides
            run = result.get("run")
            claims = [claim for claim in result.get("claims") or () if isinstance(claim, dict)]
            if isinstance(run, dict):
                resolution_bundles[index] = {"claims": claims, "run": dict(run)}
                for metric in (
                    "request_count",
                    "web_search_call_count",
                    "cache_hits",
                    "elapsed_ms",
                ):
                    resolution_metrics[metric] += int(run.get(metric) or 0)
                for key, value in dict(run.get("token_usage") or {}).items():
                    resolution_metrics["token_usage"][str(key)] = int(
                        resolution_metrics["token_usage"].get(str(key), 0)
                    ) + int(value or 0)

    final_records: list[tuple[int, dict[str, Any], Any, dict[str, Any]]] = []
    for index, row, _initial_receipt in initial_records:
        overrides = resolution_overrides.get(index, {})
        payload = dict(row)
        if overrides:
            payload["condition_results"] = overrides
        try:
            if service is None:
                _mark_construction_failure(index, row, "receipt service unavailable")
                continue
            receipt = service.build_receipt(
                payload,
                decision_at=decision_at,
                condition_overrides=overrides or None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            _mark_construction_failure(
                index, row, f"final receipt construction failed: {type(exc).__name__}"
            )
            continue
        final_records.append((index, row, receipt, overrides))

    if final_records and service is not None:
        try:
            batch = service.evaluate_candidates(
                [
                    ({**row, "condition_results": overrides} if overrides else dict(row))
                    for _index, row, _receipt, overrides in final_records
                ],
                decision_at=decision_at,
            )
            final_records = [
                (item[0], item[1], receipt, item[3])
                for item, receipt in zip(final_records, batch, strict=True)
            ]
        except (KeyError, TypeError, ValueError):
            pass

    persisted_count = 0
    reused_count = 0
    for index, row, receipt, _overrides in final_records:
        specs = {spec.condition_id: spec for spec in registry_for_strategy(receipt.strategy_id)}
        result_by_id = {result.condition_id: result for result in receipt.condition_results}
        core_categories = {
            ConditionCategory.HARD_MARKET,
            ConditionCategory.HARD_RISK,
            ConditionCategory.STRATEGY_CORE,
        }
        core_passed = [
            condition_id
            for condition_id, spec in specs.items()
            if spec.category in core_categories
            and result_by_id[condition_id].status
            in {ConditionStatus.PASS, ConditionStatus.RESOLVED_FROM_SOURCE}
        ]
        ai_evidence = [
            {
                "condition_id": result.condition_id,
                "status": getattr(result.status, "value", str(result.status)),
                "source_urls": list(result.source_urls)[:2],
                "source_hashes": list(result.source_hashes)[:2],
            }
            for result in receipt.condition_results
            if specs[result.condition_id].category == ConditionCategory.AI_RESOLVABLE
            and result.status == ConditionStatus.RESOLVED_FROM_SOURCE
        ]
        unresolved_final = [
            spec.condition_id
            for spec in specs.values()
            if spec.category in resolvable_categories
            and result_by_id[spec.condition_id].status in unresolved_statuses
        ]
        tier = getattr(receipt.pick_tier, "value", str(receipt.pick_tier))
        why = (
            "all research-blocking conditions passed"
            if receipt.research_pick_eligible and not receipt.disclosed_gaps
            else (
                "research eligible with disclosed gaps"
                if receipt.research_pick_eligible
                else f"blocked by {receipt.first_blocking_failure or 'deterministic policy'}"
            )
        )
        disagreements = list(row.get("strategy_receipt_disagreement") or [])
        if bool(row.get("strategy_receipt_legacy_can_alert")) != receipt.research_pick_eligible:
            if "legacy_vs_receipt_alert_disposition" not in disagreements:
                disagreements.append("legacy_vs_receipt_alert_disposition")
        row.update(
            {
                "receipt_id": receipt.receipt_id,
                "receipt_hash_sha256": receipt.receipt_hash_sha256,
                "strategy_decision_receipt": receipt.to_dict(),
                "pick_tier": tier,
                "research_pick_eligible": receipt.research_pick_eligible,
                "paper_entry_eligible": receipt.paper_entry_eligible,
                "disclosed_gaps": list(receipt.disclosed_gaps),
                "first_blocking_failure": receipt.first_blocking_failure,
                "all_blocking_failures": list(receipt.all_blocking_failures),
                "condition_results": [result.to_dict() for result in receipt.condition_results],
                "core_conditions_passed": core_passed,
                "ai_resolved_evidence": ai_evidence,
                "reward_risk_ratio": receipt.reward_risk_ratio,
                "strategy_receipt_tier": tier,
                "strategy_receipt_research_pick_eligible": receipt.research_pick_eligible,
                "strategy_receipt_paper_entry_eligible": receipt.paper_entry_eligible,
                "strategy_receipt_unresolved_conditions": unresolved_final,
                "strategy_receipt_construction_status": "COMPLETE",
                "strategy_receipt_gap": "",
                "strategy_receipt_disagreement": disagreements,
                "receipt_reason": why,
                "research_only": receipt.research_only,
                "broker_execution_enabled": receipt.broker_execution_enabled,
            }
        )
        if receipt.strategy_id == "alphaops_v5" and receipt.paper_entry_eligible:
            modeled_cost = _build_modeled_cost_receipt(row)
            if modeled_cost is not None:
                row["modeled_cost_receipt"] = modeled_cost
        bundle = resolution_bundles.get(index, {})
        run = bundle.get("run")
        if isinstance(run, dict) and str(run.get("status") or "") != "cache_hit":
            run = dict(run)
            if str(run.get("run_id") or "").startswith("unavailable-"):
                run["run_id"] = f"unavailable-{receipt.receipt_id}"
        inserted = store.persist_strategy_decision_receipt(
            receipt,
            evidence_claims=bundle.get("claims") or (),
            resolution_run=run,
        )
        row["strategy_receipt_persistence_status"] = (
            "PERSISTED" if inserted else "REUSED"
        )
        if inserted:
            persisted_count += 1
        else:
            reused_count += 1

    stats = {
        "status": "shadow_only" if shadow_only else "non_shadow_research_only",
        "computed": len(final_records),
        "persisted": persisted_count,
        "reused": reused_count,
        "eligible_candidates": len(eligible_rows),
        "uncovered_candidates": len(uncovered),
        "uncovered": uncovered,
        "construction_records": list(construction_records.values()),
        "build_errors": list(construction_records.values()),
        "resolution_budget": resolution_budget,
        "resolution_candidates": len(selected_initial_records),
        "resolver_candidates": len(selected_resolution_candidates),
        "selected_candidates": len(selected_initial_records),
        "resolution_deferred": deferred_count,
        "resolver_deferred": resolution_deferred_count,
        "resolution_metrics": resolution_metrics,
        "legacy_selection_unchanged": True,
        "policy_mutation": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    source_summary["strategy_decision_receipts"] = stats
    return stats


def _build_modeled_cost_receipt(row: dict[str, Any]) -> dict[str, Any] | None:
    """Create an exact-policy after-cost receipt for a frozen v5 plan."""

    plan = row.get("alphaops_market_structure_plan")
    if not isinstance(plan, dict) or str(plan.get("status") or "") != COMPLETE:
        return None
    try:
        direction = str(plan.get("direction") or "").lower()
        entry = float(plan["entry"])
        stop = float(plan["stop"])
        target = float(plan["target"])
    except (KeyError, TypeError, ValueError):
        return None
    policy = DEFAULT_V5_POLICY
    if direction == "long":
        expected_entry = entry * (1 + policy.entry_slippage_bps / 10_000)
        expected_stop = stop * (1 - policy.exit_slippage_bps / 10_000)
        expected_target = target * (1 - policy.exit_slippage_bps / 10_000)
    elif direction == "short":
        expected_entry = entry * (1 - policy.entry_slippage_bps / 10_000)
        expected_stop = stop * (1 + policy.exit_slippage_bps / 10_000)
        expected_target = target * (1 + policy.exit_slippage_bps / 10_000)
    else:
        return None
    commission = policy.commission_per_share_per_side * 2
    if direction == "long":
        reward = expected_target - expected_entry - commission
        risk = expected_entry - expected_stop + commission
    else:
        reward = expected_entry - expected_target - commission
        risk = expected_stop - expected_entry + commission
    ratio = reward / risk if reward > 0 and risk > 0 else None
    if ratio is None:
        return None
    payload = {
        "schema_version": "dawnstrike.alphaops.modeled_cost_receipt.v1",
        "plan_hash_sha256": str(plan.get("plan_hash_sha256") or ""),
        "direction": direction,
        "cost_model_version": policy.cost_model_version,
        "entry_slippage_bps": policy.entry_slippage_bps,
        "exit_slippage_bps": policy.exit_slippage_bps,
        "commission_per_share_per_side": policy.commission_per_share_per_side,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "expected_entry_price": round(expected_entry, 8),
        "expected_stop_exit_price": round(expected_stop, 8),
        "expected_target_exit_price": round(expected_target, 8),
        "risk_per_share_after_cost": round(risk, 8),
        "reward_per_share_after_cost": round(reward, 8),
        "after_cost_reward_risk": round(ratio, 8),
        "research_only": True,
        "broker_execution": "disabled",
    }
    payload["receipt_hash_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    return payload


def _apply_receipt_risk_gates(
    signals: list[dict[str, Any]], feature_vectors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    feature_by_ticker = {str(row.get("ticker") or "").upper(): row for row in feature_vectors}
    for row in signals:
        legacy_can_alert = row.get("can_alert")
        legacy_no_trade_reason = str(row.get("no_trade_reason") or "")
        decision = evaluate_risk(
            row,
            feature_by_ticker.get(str(row.get("ticker") or "").upper(), {}),
        )
        row.update(decision.to_dict())
        row["research_only"] = True
        row["broker_execution_enabled"] = False
        disagreements = list(row.get("strategy_receipt_disagreement") or [])
        for reason in decision.strategy_receipt_disagreement or []:
            if reason not in disagreements:
                disagreements.append(reason)
        row["strategy_receipt_disagreement"] = disagreements
        if bool(row.get("strategy_receipt_shadow_only")):
            row["can_alert"] = legacy_can_alert
            row["no_trade_reason"] = legacy_no_trade_reason
        elif not decision.can_alert:
            row["no_trade_reason"] = ";".join(
                dict.fromkeys(
                    [
                        item
                        for item in [legacy_no_trade_reason, *decision.hard_avoid_reasons]
                        if item
                    ]
                )
            )
    return signals


def _notification_event(
    run_id: str,
    hint: str,
    title: str,
    body: str,
    *,
    payload: dict[str, Any] | None = None,
) -> NotificationEvent:
    return NotificationEvent(
        event_key=f"alphaops:{run_id}:{hint}",
        title=title,
        body=body,
        channel_hint=hint,
        payload={
            "run_id": run_id,
            "source": "alphaops",
            "telegram_compact_message": body,
            **dict(payload or {}),
        },
    )


def _official_selection_notification_event(
    run_id: str,
    hint: str,
    title: str,
    body: str,
    *,
    selected_signals: list[dict[str, Any]],
    research_signals: list[dict[str, Any]] | None = None,
) -> NotificationEvent:
    """Keep official cohort members separate from labeled research radar rows."""

    return _notification_event(
        run_id,
        hint,
        title,
        body,
        payload={
            "signals": list(selected_signals),
            "research_radar": list(research_signals or []),
        },
    )


def _load_frozen_official_notification_manifest(
    store: SQLiteScanStore,
    *,
    selected_at: str,
) -> dict[str, Any] | None:
    """Load the persisted notification body before rendering a retry."""

    strategy_id, strategy_version = alphaops_strategy_contract(selected_at)
    existing = store.load_official_strategy_cohort(
        market_date=selected_at[:10],
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        cohort=ALPHAOPS_OFFICIAL_COHORT,
    )
    payload = existing.get("payload_json") if isinstance(existing, dict) else None
    manifest = payload.get("notification_manifest") if isinstance(payload, dict) else None
    if not isinstance(manifest, dict) or not str(manifest.get("body") or ""):
        return None
    return dict(manifest)


def _govern_frozen_official_cohort_retry(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    selected_signals: list[dict[str, Any]],
    decision: dict[str, Any],
    selected_at: str,
    event: NotificationEvent,
) -> dict[str, Any] | None:
    """Reuse one immutable date cohort after a pre-dispatch persistence crash.

    The producer-attempt scan may change, but the operator-facing event and its
    exact members may not.  The original event body is persisted inside the
    cohort payload on new writes; legacy rows may reuse a deterministically
    re-rendered body only when its SHA-256 is already the frozen body hash.
    """

    strategy_id, strategy_version = alphaops_strategy_contract(selected_at)
    existing = store.load_official_strategy_cohort(
        market_date=selected_at[:10],
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        cohort=ALPHAOPS_OFFICIAL_COHORT,
    )
    if existing is None:
        return None
    rows = store.load_signal_selections(
        scan_id=str(existing.get("scan_id") or ""),
        event_key=str(existing.get("event_key") or ""),
        strategy_id=strategy_id,
        cohort=ALPHAOPS_OFFICIAL_COHORT,
        limit=100,
    )
    if (
        not rows
        or membership_sha256(rows) != str(existing.get("membership_sha256") or "")
    ):
        raise SnapshotValidationError(
            "FROZEN_COHORT_CONFLICT: persisted official membership is incomplete "
            "or does not match its immutable hash"
        )
    decision_name = (
        "no_trade" if decision.get("no_trade") else str(decision.get("decision_tier") or "selected")
    )
    if any(str(row.get("decision") or "") != decision_name for row in rows):
        raise SnapshotValidationError(
            "FROZEN_COHORT_CONFLICT: retry decision differs from the immutable cohort"
        )
    current_members = sorted(
        (
            _selection_signal_id(signal, scan_id),
            str(signal.get("ticker") or "").upper(),
        )
        for signal in selected_signals
        if _selection_signal_id(signal, scan_id)
    )
    frozen_members = sorted(
        (str(row.get("signal_id") or ""), str(row.get("ticker") or "").upper())
        for row in rows
    )
    if decision_name == "no_trade":
        current_is_no_trade = (
            len(selected_signals) == 1
            and str(selected_signals[0].get("ticker") or "").upper() == "NO_TRADE"
        )
        frozen_is_no_trade = (
            len(frozen_members) == 1 and frozen_members[0][1] == "NO_TRADE"
        )
        membership_matches = current_is_no_trade and frozen_is_no_trade
    else:
        membership_matches = bool(current_members) and current_members == frozen_members
    if not membership_matches:
        raise SnapshotValidationError(
            "FROZEN_COHORT_CONFLICT: retry members differ from the immutable cohort"
        )

    # A producer-attempt scan may change, but the source scan represented by
    # each selected signal may not.  Stable signal IDs alone are insufficient:
    # an adversarial retry can otherwise reuse an ID with a different source
    # payload and silently cross-join the watcher to new historical truth.
    if decision_name != "no_trade":
        frozen_by_signal_id = {
            str(row.get("signal_id") or ""): row for row in rows
        }
        for signal in selected_signals:
            signal_id = _selection_signal_id(signal, scan_id)
            frozen_row = frozen_by_signal_id.get(signal_id)
            if frozen_row is None:
                continue
            frozen_payload = frozen_row.get("payload_json")
            frozen_signal = (
                frozen_payload.get("signal")
                if isinstance(frozen_payload, dict)
                else None
            )
            frozen_source_scan_id = (
                str(frozen_signal.get("scan_id") or "")
                if isinstance(frozen_signal, dict)
                else ""
            )
            if not frozen_source_scan_id and isinstance(frozen_payload, dict):
                frozen_source_scan_id = str(
                    frozen_payload.get("source_scan_id")
                    or (
                        frozen_payload.get("frozen_slate_lineage", {}).get(
                            "frozen_source_scan_id"
                        )
                        if isinstance(frozen_payload.get("frozen_slate_lineage"), dict)
                        else ""
                    )
                    or ""
                )
            current_lineage = signal.get("frozen_slate_lineage")
            current_source_scan_id = str(
                signal.get("source_scan_id")
                or (
                    current_lineage.get("frozen_source_scan_id")
                    if isinstance(current_lineage, dict)
                    else ""
                )
                or signal.get("scan_id")
                or ""
            )
            if not frozen_source_scan_id or not current_source_scan_id:
                raise SnapshotValidationError(
                    "FROZEN_COHORT_CONFLICT: frozen and retry source scan "
                    "identities are required for every official selection"
                )
            if current_source_scan_id != frozen_source_scan_id:
                raise SnapshotValidationError(
                    "FROZEN_COHORT_CONFLICT: retry source scan differs from the "
                    "immutable official selection"
                )

    body_hash = _body_sha256(event.body)
    if body_hash != str(existing.get("body_sha256") or ""):
        raise SnapshotValidationError(
            "FROZEN_COHORT_CONFLICT: retry rendered body differs from the immutable cohort"
        )
    cohort_payload = existing.get("payload_json")
    notification_manifest = (
        cohort_payload.get("notification_manifest")
        if isinstance(cohort_payload, dict)
        else None
    )
    if isinstance(notification_manifest, dict):
        frozen_body = str(notification_manifest.get("body") or "")
        if (
            str(notification_manifest.get("schema_version") or "")
            != "dawnstrike.alphaops.official_notification_manifest.v1"
            or str(notification_manifest.get("event_key") or "")
            != str(existing.get("event_key") or "")
            or str(notification_manifest.get("body_sha256") or "")
            != str(existing.get("body_sha256") or "")
            or _body_sha256(frozen_body) != str(existing.get("body_sha256") or "")
            or str(notification_manifest.get("title") or "") != event.title
            or str(notification_manifest.get("channel_hint") or "") != event.channel_hint
            or notification_manifest.get("research_only") is not True
            or notification_manifest.get("broker_execution_enabled") is not False
        ):
            raise SnapshotValidationError(
                "FROZEN_COHORT_CONFLICT: immutable notification manifest is invalid"
            )
    else:
        frozen_body = event.body
    frozen_signals = [
        dict(row.get("payload_json", {}).get("signal") or {})
        for row in rows
        if isinstance(row.get("payload_json"), dict)
    ]
    retry_payload = {
        **dict(event.payload or {}),
        "run_id": str(existing.get("scan_id") or ""),
        "producer_attempt_scan_id": scan_id,
        "frozen_cohort_retry": {
            "status": "GOVERNED_IMMUTABLE_RETRY",
            "official_cohort_id": str(existing.get("official_cohort_id") or ""),
            "source_scan_id": str(existing.get("scan_id") or ""),
            "producer_attempt_scan_id": scan_id,
            "membership_sha256": str(existing.get("membership_sha256") or ""),
        },
        "signals": frozen_signals,
    }
    governed_event = NotificationEvent(
        event_key=str(existing.get("event_key") or ""),
        title=event.title,
        body=frozen_body,
        channel_hint=event.channel_hint,
        ticker=event.ticker,
        payload=retry_payload,
    )
    return {
        "event": governed_event,
        "selections": rows,
        "scan_id": str(existing.get("scan_id") or ""),
        "selected_at": str(existing.get("claimed_at") or ""),
        "stats": {
            "inserted": 0,
            "skipped": len(rows),
            "official_cohort_claimed": False,
            "official_cohort_reused": True,
            "official_cohort_id": str(existing.get("official_cohort_id") or ""),
            "official_membership_sha256": str(existing.get("membership_sha256") or ""),
            "producer_attempt_scan_id": scan_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "cohort": ALPHAOPS_OFFICIAL_COHORT,
        },
    }


def _persist_official_selections(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    selected_signals: list[dict[str, Any]],
    decision: dict[str, Any],
    selected_at: str,
    event: NotificationEvent,
    slate: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Freeze the exact signal identities represented by one operator message."""

    strategy_id, strategy_version = alphaops_strategy_contract(selected_at)
    decision_name = (
        "no_trade" if decision.get("no_trade") else str(decision.get("decision_tier") or "selected")
    )
    body_sha256 = _body_sha256(event.body)
    frozen_slate = dict(slate or {})
    frozen_rows_by_id: dict[str, dict[str, Any]] = {}
    frozen_lineage: dict[str, Any] = {}
    frozen_source_scan_id = ""
    reuse_status = ""
    if frozen_slate:
        validate_ranked_research_slate(
            frozen_slate,
            market_date=selected_at[:10],
        )
        frozen_source_scan_id = str(frozen_slate.get("scan_id") or "")
        reuse_status = (
            "CURRENT_SCAN"
            if frozen_source_scan_id == scan_id
            else "GOVERNED_DAILY_FREEZE_REUSE"
        )
        frozen_lineage = {
            "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
            "slate_id": str(frozen_slate.get("slate_id") or ""),
            "slate_content_hash_sha256": str(
                frozen_slate.get("content_hash_sha256") or ""
            ),
            "frozen_source_scan_id": frozen_source_scan_id,
            "current_scan_id": scan_id,
            "reuse_status": reuse_status,
        }
        frozen_rows_by_id = {
            str(row.get("research_selection_id") or ""): dict(row)
            for row in frozen_slate.get("rows") or []
            if str(row.get("research_selection_id") or "")
        }
    rows: list[dict[str, Any]] = []
    for signal in selected_signals:
        signal_id = _selection_signal_id(signal, scan_id)
        if not signal_id:
            continue
        signal_payload = dict(signal)
        selection_lineage: dict[str, Any] = {}
        is_no_trade = str(signal.get("ticker") or "").upper() == "NO_TRADE"
        if frozen_slate and not is_no_trade:
            research_selection_id = str(
                signal.get("research_selection_id") or ""
            )
            frozen_signal = frozen_rows_by_id.get(research_selection_id)
            if (
                frozen_signal is None
                or _selection_signal_id(frozen_signal, frozen_source_scan_id) != signal_id
                or str(frozen_signal.get("ticker") or "").upper()
                != str(signal.get("ticker") or "").upper()
            ):
                raise SnapshotValidationError(
                    "Official selection does not bind one exact immutable slate row."
                )
            signal_payload = frozen_signal
            selection_lineage = {
                "source_scan_id": frozen_source_scan_id,
                "scan_lineage_status": reuse_status,
                "frozen_slate_lineage": frozen_lineage,
                "frozen_ranked_research_slate": frozen_slate,
            }
        if (
            not is_no_trade
            and not str(signal_payload.get("scan_id") or "").strip()
            and not frozen_source_scan_id.strip()
        ):
            raise SnapshotValidationError(
                "FROZEN_COHORT_CONFLICT: official selection lacks a source scan identity"
            )
        identity = f"{strategy_id}|{strategy_version}|{ALPHAOPS_OFFICIAL_COHORT}|{signal_id}"
        selection_id = f"selection:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        row = {
            "selection_id": selection_id,
            "scan_id": scan_id,
            "signal_id": signal_id,
            "ticker": str(signal.get("ticker") or "").upper(),
            "rank": signal.get("rank"),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "cohort": ALPHAOPS_OFFICIAL_COHORT,
            "decision": decision_name,
            "selected_at": selected_at,
            "event_key": event.event_key,
            "body_sha256": body_sha256,
            **{
                key: value
                for key, value in selection_lineage.items()
                if key in {"source_scan_id", "scan_lineage_status"}
            },
        }
        row["payload_json"] = {
            **row,
            "decision_payload": decision,
            "signal": signal_payload,
            "publication_row": signal,
            **selection_lineage,
            "research_only": True,
            "broker_execution_enabled": False,
        }
        rows.append(row)
    strategy_stats = store.persist_strategy_versions(
        [
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "registered_at": selected_at,
                "definition_json": {
                    "name": "AlphaOps v5" if strategy_id == "alphaops_v5" else "AlphaOps v4",
                    "cohort": ALPHAOPS_OFFICIAL_COHORT,
                    "decision_source": "governed_official_cohort",
                    "model_version": ALPHA_MODEL_VERSION,
                    "prospective_contract": strategy_id == "alphaops_v5",
                    "research_only": True,
                    "broker_execution_enabled": False,
                },
                "payload_json": {
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "model_version": ALPHA_MODEL_VERSION,
                    "cohort": ALPHAOPS_OFFICIAL_COHORT,
                    "registered_at": selected_at,
                    "research_only": True,
                },
            }
        ]
    )
    cohort_row = build_official_cohort_row(
        market_date=selected_at[:10],
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        scan_id=scan_id,
        event_key=event.event_key,
        body_sha256=body_sha256,
        claimed_at=selected_at,
        selections=rows,
    )
    cohort_row["payload_json"]["notification_manifest"] = {
        "schema_version": "dawnstrike.alphaops.official_notification_manifest.v1",
        "event_key": event.event_key,
        "title": event.title,
        "body": event.body,
        "channel_hint": event.channel_hint,
        "body_sha256": body_sha256,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    cohort_stats = store.persist_official_signal_cohort(cohort_row, rows)
    inserted = int(cohort_stats["inserted_members"])
    return rows, {
        "inserted": inserted,
        "skipped": len(rows) - inserted,
        "official_cohort_claimed": bool(cohort_stats["claimed"]),
        "official_cohort_id": cohort_row["official_cohort_id"],
        "official_membership_sha256": cohort_row["membership_sha256"],
        "strategy_versions_inserted": strategy_stats["inserted"],
        "strategy_versions_skipped": strategy_stats["skipped"],
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "cohort": ALPHAOPS_OFFICIAL_COHORT,
    }


def _persist_research_radar_selections(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    radar: list[dict[str, Any]],
    slate: dict[str, Any],
    selected_at: str,
    event: NotificationEvent,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Persist the exact conditional radar plans represented in Telegram."""

    existing_rows = store.load_signal_selections(
        scan_id=scan_id,
        event_key=event.event_key,
        strategy_id=ALPHAOPS_RADAR_COHORT,
        cohort=ALPHAOPS_RADAR_COHORT,
        limit=max(100, len(radar) * 2, 1),
    )
    if not radar:
        if existing_rows:
            raise SnapshotValidationError(
                "FROZEN_COHORT_CONFLICT: retry removed immutable research radar members"
            )
        return [], {
            "inserted": 0,
            "skipped": 0,
            "cohort": ALPHAOPS_RADAR_COHORT,
            "strategy_version": ALPHAOPS_RADAR_VERSION,
        }
    body_sha256 = _body_sha256(event.body)
    frozen_source_scan_id = str(slate.get("scan_id") or "")
    if not frozen_source_scan_id:
        raise SnapshotValidationError("Frozen research slate has no source scan identity")
    reuse_status = (
        "CURRENT_SCAN"
        if frozen_source_scan_id == scan_id
        else "GOVERNED_DAILY_FREEZE_REUSE"
    )
    frozen_lineage = {
        "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
        "slate_id": str(slate.get("slate_id") or ""),
        "slate_content_hash_sha256": str(slate.get("content_hash_sha256") or ""),
        "frozen_source_scan_id": frozen_source_scan_id,
        "current_scan_id": scan_id,
        "reuse_status": reuse_status,
    }
    rows: list[dict[str, Any]] = []
    for signal in radar:
        signal_id = _selection_signal_id(signal, scan_id)
        if not signal_id:
            continue
        identity = f"{ALPHAOPS_RADAR_COHORT}|{ALPHAOPS_RADAR_VERSION}|{scan_id}|{signal_id}"
        row = {
            "selection_id": f"selection:{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
            "scan_id": scan_id,
            "source_scan_id": frozen_source_scan_id,
            "scan_lineage_status": reuse_status,
            "signal_id": signal_id,
            "ticker": str(signal.get("ticker") or "").upper(),
            "rank": signal.get("rank"),
            "strategy_id": ALPHAOPS_RADAR_COHORT,
            "strategy_version": ALPHAOPS_RADAR_VERSION,
            "cohort": ALPHAOPS_RADAR_COHORT,
            "decision": "conditional_paper_watch",
            "selected_at": selected_at,
            "event_key": event.event_key,
            "body_sha256": body_sha256,
        }
        row["payload_json"] = {
            **row,
            "signal": signal,
            "frozen_slate_lineage": frozen_lineage,
            "frozen_ranked_research_slate": slate,
            "research_only": True,
            "broker_execution_enabled": False,
        }
        rows.append(row)
    if existing_rows:
        # Validate before INSERT OR IGNORE.  This keeps a conflicting retry
        # from leaving replacement rows beside the original frozen radar.
        if len(existing_rows) != len(rows):
            raise SnapshotValidationError(
                "FROZEN_COHORT_CONFLICT: research selection membership changed"
            )
        stored_by_id = {
            str(row.get("selection_id") or ""): row for row in existing_rows
        }
        for expected in rows:
            actual = stored_by_id.get(str(expected.get("selection_id") or ""))
            expected_stored = {
                key: expected.get(key)
                for key in (
                    "selection_id",
                    "scan_id",
                    "signal_id",
                    "ticker",
                    "rank",
                    "strategy_id",
                    "strategy_version",
                    "cohort",
                    "decision",
                    "selected_at",
                    "event_key",
                    "body_sha256",
                    "payload_json",
                )
            }
            if actual is None or canonical_json(actual) != canonical_json(expected_stored):
                raise SnapshotValidationError(
                    "FROZEN_COHORT_CONFLICT: research selection identity was already "
                    "persisted with different immutable truth"
                )
        stored_rows = existing_rows
        persisted = {"inserted": 0, "skipped": len(rows)}
    else:
        try:
            persisted = store.persist_signal_selections(rows, require_exact=True)
        except StorageError as exc:
            raise SnapshotValidationError(
                "FROZEN_COHORT_CONFLICT: research selection persistence rejected "
                "an incomplete or conflicting immutable set"
            ) from exc
        stored_rows = store.load_signal_selections(
            scan_id=scan_id,
            event_key=event.event_key,
            strategy_id=ALPHAOPS_RADAR_COHORT,
            cohort=ALPHAOPS_RADAR_COHORT,
            limit=max(100, len(rows) * 2),
        )
        if len(stored_rows) != len(rows):
            raise SnapshotValidationError(
                "FROZEN_COHORT_CONFLICT: research selection persistence is incomplete"
            )
        stored_by_id = {str(row.get("selection_id") or ""): row for row in stored_rows}
        for expected in rows:
            actual = stored_by_id.get(str(expected.get("selection_id") or ""))
            expected_stored = {
                key: expected.get(key)
                for key in (
                    "selection_id",
                    "scan_id",
                    "signal_id",
                    "ticker",
                    "rank",
                    "strategy_id",
                    "strategy_version",
                    "cohort",
                    "decision",
                    "selected_at",
                    "event_key",
                    "body_sha256",
                    "payload_json",
                )
            }
            if actual is None or canonical_json(actual) != canonical_json(expected_stored):
                raise SnapshotValidationError(
                    "FROZEN_COHORT_CONFLICT: research selection identity was already "
                    "persisted with different immutable truth"
                )
    strategy_stats = store.persist_strategy_versions(
        [
            {
                "strategy_id": ALPHAOPS_RADAR_COHORT,
                "strategy_version": ALPHAOPS_RADAR_VERSION,
                "registered_at": selected_at,
                "definition_json": {
                    "name": "Dawnstrike conditional research radar",
                    "cohort": ALPHAOPS_RADAR_COHORT,
                    "minimum_reward_risk": 1.5,
                    "maximum_stop_distance_pct": 8.0,
                    "maximum_spread_pct": 3.0,
                    "research_only": True,
                    "broker_execution_enabled": False,
                },
                "payload_json": {
                    "strategy_id": ALPHAOPS_RADAR_COHORT,
                    "strategy_version": ALPHAOPS_RADAR_VERSION,
                    "registered_at": selected_at,
                    "research_only": True,
                },
            }
        ]
    )
    return stored_rows, {
        **persisted,
        "cohort": ALPHAOPS_RADAR_COHORT,
        "strategy_version": ALPHAOPS_RADAR_VERSION,
        "strategy_versions_inserted": strategy_stats["inserted"],
        "strategy_versions_skipped": strategy_stats["skipped"],
    }


def _selection_signal_id(signal: dict[str, Any], scan_id: str) -> str:
    signal_id = str(signal.get("signal_id") or signal.get("signal_key") or "").strip()
    if signal_id:
        return signal_id
    ticker = str(signal.get("ticker") or "").upper()
    rank = signal.get("rank")
    if not ticker or rank in {None, ""}:
        return ""
    return f"{scan_id}:{rank}:{ticker}"


def _historical_publication_rows(
    current_signals: list[dict[str, Any]],
    frozen_publication_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Materialize frozen rows before delivery, including crash-retry recovery."""

    rows: list[dict[str, Any]] = []
    seen_signal_ids: set[str] = set()
    for row in [*current_signals, *frozen_publication_rows]:
        signal_id = str(row.get("signal_id") or row.get("signal_key") or "").strip()
        if not signal_id:
            signal_id = _selection_signal_id(row, str(row.get("scan_id") or ""))
        if not signal_id or signal_id in seen_signal_ids:
            continue
        rows.append(dict(row))
        seen_signal_ids.add(signal_id)
    return rows


def _notification_channels(notify: str) -> list[str]:
    channels = [channel.strip().lower() for channel in notify.split(",") if channel.strip()]
    return channels or ["console"]


def _existing_notification_keys(
    store: SQLiteScanStore,
    *,
    events: list[NotificationEvent],
    notify: str,
) -> set[str]:
    existing: set[str] = set()
    channels = set(_notification_channels(notify)) | {"console"}
    for event in events:
        for channel in channels:
            notification_key = f"{event.event_key}:{channel}"
            if store.has_notification(notification_key):
                existing.add(notification_key)
    return existing


def _persist_notification_delivery_memberships(
    store: SQLiteScanStore,
    *,
    selections: list[dict[str, Any]],
    events: list[NotificationEvent],
    notify: str,
    preexisting_notification_keys: set[str],
) -> list[dict[str, Any]]:
    """Record exact members and preserve delivery truth across deduplicated runs."""

    selections = _canonical_delivery_selections(selections)
    attempted_at = utc_now_iso()
    rows: list[dict[str, Any]] = []
    for event in events:
        event_selections = [
            row for row in selections if str(row.get("event_key") or "") == event.event_key
        ]
        for channel in _notification_channels(notify):
            notification_key = f"{event.event_key}:{channel}"
            notification = store.load_notification(notification_key)
            actual_notification_key = notification_key
            if notification is None and channel == "telegram":
                console_key = f"{event.event_key}:console"
                console_notification = store.load_notification(console_key)
                if console_notification is not None and console_notification.get("dry_run"):
                    notification = console_notification
                    actual_notification_key = console_key
            stored_body = str((notification or {}).get("body") or event.body)
            if notification is None:
                delivery_status = "failed"
                delivered_at = ""
            elif notification.get("dry_run"):
                delivery_status = "dry_run"
                delivered_at = ""
            else:
                delivery_status = "delivered"
                delivered_at = str(notification.get("sent_at") or "")
            attempt_status = (
                "deduplicated"
                if actual_notification_key in preexisting_notification_keys
                else delivery_status
            )
            body_sha256 = _body_sha256(stored_body)
            for selection in event_selections:
                identity = f"{event.event_key}|{channel}|{selection['signal_id']}"
                membership_id = f"delivery:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
                delivery_row: dict[str, Any] = {
                    "membership_id": membership_id,
                    "selection_id": str(selection["selection_id"]),
                    "scan_id": str(selection["scan_id"]),
                    "signal_id": str(selection["signal_id"]),
                    "ticker": str(selection.get("ticker") or "").upper(),
                    "strategy_id": str(selection["strategy_id"]),
                    "strategy_version": str(selection["strategy_version"]),
                    "cohort": str(selection["cohort"]),
                    "decision": str(selection["decision"]),
                    "selected_at": str(selection["selected_at"]),
                    "event_key": event.event_key,
                    "channel": channel,
                    "delivery_status": delivery_status,
                    "attempted_at": attempted_at,
                    "delivered_at": delivered_at,
                    "body_sha256": body_sha256,
                }
                delivery_row["payload_json"] = {
                    **delivery_row,
                    "notification_key": notification_key,
                    "recorded_notification_key": actual_notification_key,
                    "attempt_status": attempt_status,
                    "deduplicated": attempt_status == "deduplicated",
                    "legacy_recovery": dict(event.payload or {}).get("legacy_recovery"),
                    "body": stored_body,
                    "research_only": True,
                }
                rows.append(delivery_row)
    store.persist_notification_deliveries(rows)
    event_keys = {event.event_key for event in events}
    return [
        row
        for row in store.load_notification_deliveries(
            scan_id=str(selections[0]["scan_id"]) if selections else "",
            cohort=ALPHAOPS_OFFICIAL_COHORT,
            limit=max(100, len(rows) * 2),
        )
        if row["event_key"] in event_keys
    ]


def _canonical_delivery_selections(
    selections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one immutable delivery member per event/channel/signal.

    Official membership is the canonical notification identity when the same
    ticker is also present in the research radar.  Radar persistence remains
    separate; only its duplicate delivery projection is excluded.
    """

    by_event_signal: dict[tuple[str, str], dict[str, Any]] = {}
    for selection in selections:
        key = (
            str(selection.get("event_key") or ""),
            str(selection.get("signal_id") or ""),
        )
        if not key[0] or not key[1]:
            continue
        previous = by_event_signal.get(key)
        if previous is None:
            by_event_signal[key] = selection
            continue
        previous_cohort = str(previous.get("cohort") or "")
        current_cohort = str(selection.get("cohort") or "")
        if previous_cohort == ALPHAOPS_OFFICIAL_COHORT:
            if current_cohort == ALPHAOPS_RADAR_COHORT:
                continue
        elif current_cohort == ALPHAOPS_OFFICIAL_COHORT:
            by_event_signal[key] = selection
            continue
        if canonical_json(previous) != canonical_json(selection):
            raise SnapshotValidationError(
                "FROZEN_COHORT_CONFLICT: duplicate notification membership has "
                "different immutable selection truth"
            )
    return list(by_event_signal.values())


def _body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def recover_legacy_alpha_notification_memberships(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    limit: int = 5000,
) -> dict[str, int]:
    """Conservatively recover exact legacy memberships from stored rendered bodies.

    Recovery is deliberately opt-in. A watch message is accepted only when its
    rendered pick count equals the parsed ticker lines and every ticker maps to
    exactly one historical signal in that scan. Ambiguous or truncated messages
    remain unknown rather than inheriting the old, over-broad payload membership.
    """

    store = SQLiteScanStore(db_path)
    store.initialize()
    stats = {
        "notifications_scanned": 0,
        "notifications_recovered": 0,
        "memberships_recovered": 0,
        "already_exact": 0,
        "ambiguous_or_unsupported": 0,
    }
    for notification in store.load_recent_notifications(limit=limit):
        stats["notifications_scanned"] += 1
        channel = str(notification.get("channel") or "").lower()
        qualified_event_key = str(notification.get("event_key") or "")
        channel_suffix = f":{channel}"
        if (
            channel != "telegram"
            or notification.get("dry_run")
            or not qualified_event_key.endswith(channel_suffix)
        ):
            stats["ambiguous_or_unsupported"] += 1
            continue
        event_key = qualified_event_key[: -len(channel_suffix)]
        if store.load_notification_deliveries(
            event_key=event_key,
            channel=channel,
            limit=1,
        ):
            stats["already_exact"] += 1
            continue
        run_id = str(notification.get("run_id") or "")
        body = str(notification.get("body") or "")
        hint = str(notification.get("channel_hint") or "")
        tickers = _legacy_body_tickers(body, hint)
        if not run_id or not tickers:
            stats["ambiguous_or_unsupported"] += 1
            continue
        historical = store.load_historical_signals(scan_id=run_id, limit=500)
        historical_by_ticker: dict[str, list[dict[str, Any]]] = {}
        for row in historical:
            historical_by_ticker.setdefault(str(row.get("ticker") or "").upper(), []).append(row)
        matches = [historical_by_ticker.get(ticker, []) for ticker in tickers]
        if any(len(rows) != 1 for rows in matches):
            stats["ambiguous_or_unsupported"] += 1
            continue
        matched_rows = [rows[0] for rows in matches]
        recovered_signals = [_legacy_selection_signal(row) for row in matched_rows]
        generated_at_values = [
            str(row.get("generated_at") or "")
            for row in matched_rows
            if str(row.get("generated_at") or "")
        ]
        selected_at = (
            min(generated_at_values)
            if generated_at_values
            else str(notification.get("sent_at") or utc_now_iso())
        )
        event = NotificationEvent(
            event_key=event_key,
            title=str(notification.get("title") or "Dawnstrike Alpha Watch"),
            body=body,
            channel_hint=hint,
            payload={
                "run_id": run_id,
                "source": LEGACY_ALPHAOPS_STRATEGY_ID,
                "signals": recovered_signals,
                "legacy_recovery": "exact_rendered_body",
            },
        )
        selections, _ = _persist_official_selections(
            store,
            scan_id=run_id,
            selected_signals=recovered_signals,
            decision={
                "no_trade": tickers == ["NO_TRADE"],
                "decision_tier": "legacy_body_recovered",
            },
            selected_at=selected_at,
            event=event,
        )
        deliveries = _persist_notification_delivery_memberships(
            store,
            selections=selections,
            events=[event],
            notify="telegram",
            preexisting_notification_keys=set(),
        )
        signal_ids = [str(row["signal_id"]) for row in selections]
        _link_notification_events(
            store,
            scan_id=run_id,
            events=[event],
            notify="telegram",
            dry_run=False,
            signal_ids=signal_ids,
            notification_deliveries=deliveries,
        )
        stats["notifications_recovered"] += 1
        stats["memberships_recovered"] += len(deliveries)
    return stats


def _legacy_body_tickers(body: str, hint: str) -> list[str]:
    if hint == "alpha_no_trade" and "Nothing strong enough to watch" in body:
        return ["NO_TRADE"]
    if hint != "alpha_morning_watch":
        return []
    count_match = _LEGACY_PICK_COUNT_PATTERN.search(body)
    tickers = [ticker.upper() for ticker in _LEGACY_PICK_PATTERN.findall(body)]
    if (
        count_match is None
        or int(count_match.group(1)) != len(tickers)
        or len(set(tickers)) != len(tickers)
    ):
        return []
    return tickers


def _legacy_selection_signal(row: dict[str, Any]) -> dict[str, Any]:
    raw = dict(row.get("raw_payload_json") or {})
    return {
        **raw,
        "signal_id": str(row["signal_id"]),
        "signal_key": str(row.get("alpha_signal_id") or row["signal_id"]),
        "scan_id": str(row.get("scan_id") or ""),
        "ticker": str(row.get("ticker") or "").upper(),
        "rank": row.get("rank"),
        "model_version": str(row.get("model_version") or ALPHA_MODEL_VERSION),
        "timestamp": str(row.get("generated_at") or ""),
    }


def _persisted_strategy_receipt_verifier(
    store: SQLiteScanStore,
    *,
    market_date: str,
) -> AuthenticatedStrategyReceiptResolver:
    """Resolve receipt envelopes against immutable storage before promotion."""

    return AuthenticatedStrategyReceiptResolver.from_store(
        store,
        market_date=market_date,
        strategy_id="alphaops_v5",
    )


def _dispatch(
    events: list[NotificationEvent],
    *,
    notify: str,
    db_path: str | Path,
    dry_run: bool,
) -> dict[str, int]:
    channels = [channel.strip().lower() for channel in notify.split(",") if channel.strip()]
    if not channels:
        channels = ["console"]
    config = load_config(database_path=Path(db_path), notifier_channels=",".join(channels))
    notifiers: list[BaseNotifier]
    if (
        dry_run
        and "telegram" in channels
        and not (config.telegram_bot_token and config.telegram_chat_id)
    ):
        notifiers = [ConsoleNotifier()]
    else:
        notifiers = build_notifiers(config)
    return dispatch_events(events, notifiers, SQLiteScanStore(db_path), dry_run=dry_run)


def _link_notification_events(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    events: list[NotificationEvent],
    notify: str,
    dry_run: bool,
    signal_ids: list[str],
    notification_deliveries: list[dict[str, Any]],
) -> dict[str, Any]:
    channels = _notification_channels(notify)
    channel = "telegram" if "telegram" in channels else (channels[0] if channels else "console")
    links: list[dict[str, Any]] = []
    any_alerted = False
    for event in events:
        event_key = f"{event.event_key}:{channel}"
        delivered_rows = [
            row
            for row in notification_deliveries
            if row.get("event_key") == event.event_key
            and row.get("channel") == channel
            and row.get("delivery_status") in {"delivered", "delivered_legacy"}
            and str(row.get("signal_id") or "") in signal_ids
        ]
        delivered_ids = sorted({str(row["signal_id"]) for row in delivered_rows})
        if not delivered_ids and signal_ids and not dry_run:
            notification = store.load_notification(event_key)
            if notification is not None and not notification.get("dry_run"):
                delivered_ids = sorted(set(signal_ids))
        was_alerted = channel == "telegram" and bool(delivered_ids)
        any_alerted = any_alerted or was_alerted
        updated = store.link_historical_signal_notification(
            scan_id=scan_id,
            telegram_event_key=event_key,
            was_alerted=was_alerted,
            signal_ids=delivered_ids,
        )
        delivery_by_signal = {str(row["signal_id"]): row for row in delivered_rows}
        if delivered_ids:
            event_timestamp = utc_now_iso()
            store.persist_signal_events(
                [
                    {
                        "event_id": (
                            f"{signal_id}:notification:"
                            f"{hashlib.sha256(event_key.encode()).hexdigest()[:16]}"
                        ),
                        "signal_id": signal_id,
                        "event_type": "TELEGRAM_SENT",
                        "event_timestamp": str(
                            delivery_by_signal.get(signal_id, {}).get("delivered_at")
                            or event_timestamp
                        ),
                        "event_price": None,
                        "source": channel,
                        "notes": "Exact notification membership linked to historical signal.",
                        "payload_json": {
                            "event_key": event_key,
                            "channel": channel,
                            "was_alerted": was_alerted,
                            "signal_id": signal_id,
                            "strategy_id": delivery_by_signal.get(signal_id, {}).get(
                                "strategy_id", LEGACY_ALPHAOPS_STRATEGY_ID
                            ),
                            "strategy_version": delivery_by_signal.get(signal_id, {}).get(
                                "strategy_version", ALPHA_MODEL_VERSION
                            ),
                            "cohort": delivery_by_signal.get(signal_id, {}).get(
                                "cohort", ALPHAOPS_OFFICIAL_COHORT
                            ),
                            "body_sha256": delivery_by_signal.get(signal_id, {}).get("body_sha256"),
                        },
                    }
                    for signal_id in delivered_ids
                ]
            )
        links.append({"updated": updated, "signal_count": len(delivered_ids)})
    return {
        "channel": channel,
        "was_alerted": any_alerted,
        "links": links,
    }


def _edge_label(signals: list[dict[str, Any]]) -> str:
    clean = [row for row in signals if row.get("can_alert")]
    if not clean:
        return "NONE"
    top = float(clean[0].get("alpha_score") or 0.0)
    if top >= 78:
        return "HIGH"
    if top >= 58:
        return "MEDIUM"
    return "LOW"


def _trust_gate_edge_label(watchlist: list[dict[str, Any]]) -> str:
    if not watchlist:
        return "NO CLEAN EDGE"
    statuses = {str(row.get("alert_gate_status") or "").upper() for row in watchlist}
    if statuses <= {"WATCH_ONLY", "NEEDS_CONFIRMATION"}:
        return "WATCH ONLY — NEEDS CONFIRMATION"
    if "NEEDS_CONFIRMATION" in statuses:
        return "NEEDS CONFIRMATION"
    return "PROBABILITY WATCH"


def _real_days(rows: list[dict[str, Any]]) -> int:
    dates = {
        str(
            row.get("market_date")
            or row.get("recommendation_timestamp")
            or row.get("timestamp")
            or row.get("created_at")
            or ""
        )[:10]
        for row in rows
        if str(
            row.get("market_date")
            or row.get("recommendation_timestamp")
            or row.get("timestamp")
            or row.get("created_at")
            or ""
        )[:10]
    }
    return len(dates)


def _signal_market_date(row: dict[str, Any]) -> str:
    return str(
        row.get("market_date")
        or row.get("recommendation_timestamp")
        or row.get("timestamp")
        or row.get("as_of_timestamp")
        or row.get("created_at")
        or ""
    )[:10]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_markdown(path: Path, summary: dict[str, Any]) -> None:
    truth = dict(summary.get("truth_report") or {})
    status = dict(summary.get("status") or {})
    review = dict(summary.get("daily_review_metrics") or {})
    lines = [
        "# Dawnstrike AlphaOps Report",
        "",
        "Research/watchlist only. No orders are placed.",
        "",
        f"- Model version: {status.get('model_version')}",
        f"- Real days collected: {truth.get('real_days_collected', 0)}",
        f"- Enough evidence: {truth.get('enough_evidence', False)}",
        f"- Sample size: {truth.get('sample_size', 0)}",
        f"- Win rate: {truth.get('win_rate_pct', 0)}%",
        f"- Median return: {truth.get('median_return_pct', 0)}%",
        f"- Outlier dependent: {dict(truth.get('outlier') or {}).get('outlier_dependent', False)}",
        "",
        "## Day Review Backfeed",
        "",
        f"- Review days: {review.get('review_day_count', 0)}",
        f"- Caught top mover rate: {_fmt_review_pct(review.get('caught_top_mover_rate'))}",
        f"- Missed winner rate: {_fmt_review_pct(review.get('missed_winner_rate'))}",
        f"- False positive rate: {_fmt_review_pct(review.get('false_positive_rate'))}",
        f"- Learning events proposed: {review.get('learning_events_proposed', 0)}",
        f"- Learning events applied: {review.get('learning_events_applied', 0)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _alpha_daily_review_metrics(
    runs: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    mover_count = sum(int(row.get("mover_count") or 0) for row in runs)
    signal_count = sum(int(row.get("signal_count") or 0) for row in runs)
    caught = sum(int(row.get("matched_pick_count") or 0) for row in runs)
    missed = sum(int(row.get("missed_winner_count") or 0) for row in runs)
    false_positive = sum(int(row.get("false_positive_count") or 0) for row in runs)
    return {
        "review_day_count": len(runs),
        "caught_top_mover_rate": _review_pct(caught, mover_count),
        "missed_winner_rate": _review_pct(missed, mover_count),
        "false_positive_rate": _review_pct(false_positive, signal_count),
        "learning_events_proposed": len(events),
        "learning_events_applied": sum(1 for row in events if row.get("applied")),
    }


def _review_pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 4)


def _fmt_review_pct(value: Any) -> str:
    if value in {None, ""}:
        return "Outcome Needed"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "Outcome Needed"
