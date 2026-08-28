"""Typed operational truth for the authoritative AlphaOps control plane."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION
from intraday_scanner.alpha.data_eligibility import evaluate_premarket_coverage
from intraday_scanner.services.luna_research_slate_service import (
    AuthenticatedStrategyReceiptResolver,
    apply_publication_semantics,
    build_ranked_research_slate,
    official_publication_rows,
    publication_counts,
    validate_frozen_publication_rows,
    validate_ranked_research_slate,
)


class SelectionOutcome(str, Enum):
    WATCHLIST_READY = "watchlist_ready"
    VALID_NO_EDGE = "valid_no_edge"
    REHEARSAL_COMPLETE = "rehearsal_complete"
    DATA_INELIGIBLE = "data_ineligible"
    SOURCE_FAILED = "source_failed"


@dataclass(frozen=True)
class AlphaRunContract:
    producer: str
    producer_run_id: str
    market_date: str
    model_version: str
    source_status: str
    enrichment_status: str
    ranked_count: int
    signal_count: int
    alertable_count: int
    research_candidate_count: int
    research_symbols: tuple[str, ...]
    premarket_selected_count: int
    premarket_verified_count: int
    premarket_verified_ratio: float | None
    coverage_status: str
    selection_outcome: str
    primary_veto: str
    notification_channel: str
    notification_dry_run: bool
    notification_status: str
    research_only: bool = True
    broker_execution: str = "disabled"
    # Additive Luna publication counts.  Legacy fields above remain stable for
    # consumers that have not migrated to the three-tier contract.
    source_collected: int = 0
    enrichment_selected: int = 0
    primary_verified: int = 0
    ranked_research: int = 0
    paper_plan_qualified: int = 0
    alertable_trade: int = 0
    official_selected: int = 0
    slate_shortfall_reason: str = ""
    pre_watcher_alert_gate_count: int = 0
    alertable_count_semantics: str = "legacy_pre_watcher_alert_gate"
    source_collected_count: int = 0
    enrichment_selected_count: int = 0
    primary_verified_count: int = 0
    ranked_research_count: int = 0
    paper_plan_qualified_count: int = 0
    alertable_trade_count: int = 0
    official_selected_count: int = 0
    core_universe_status: str = "DATA_UNAVAILABLE"
    core_universe_count: int = 0
    core_universe_hash_sha256: str = ""
    core_universe_market_date: str = ""
    core_snapshot_status: str = "DATA_UNAVAILABLE"
    core_snapshot_requested_count: int = 0
    core_snapshot_returned_count: int = 0
    core_snapshot_eligible_count: int = 0
    core_snapshot_fresh_count: int = 0
    core_snapshot_fresh_verified_count: int = 0
    core_snapshot_stale_count: int = 0
    core_snapshot_missing_count: int = 0
    core_snapshot_unknown_count: int = 0
    core_snapshot_duplicate_count: int = 0
    core_snapshot_coverage_status: str = "DATA_UNAVAILABLE"
    core_snapshot_coverage_receipt_ids: tuple[str, ...] = ()
    core_snapshot_coverage_receipt_hashes: tuple[str, ...] = ()
    core_snapshot_complete: bool = False
    core_index_verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    core_raw_artifact_hashes: tuple[str, ...] = ()
    core_member_set_hash_sha256: str = ""
    lane_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    lane_statuses: dict[str, dict[str, Any]] = field(default_factory=dict)
    slate_id: str = ""
    slate_content_hash_sha256: str = ""
    slate_market_date: str = ""
    slate_source_scan_id: str = ""
    slate_reuse_status: str = "UNSPECIFIED"
    slate_published_count: int = 0
    slate_selection_ids: tuple[str, ...] = ()
    strategy_contributions: dict[str, dict[str, Any]] = field(default_factory=dict)
    strategy_adapter_provenance: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "alphaops.run_contract.v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_alpha_run_contract(
    *,
    scan_id: str,
    generated_at: str,
    ranked_count: int,
    signals: list[dict[str, Any]],
    review: dict[str, Any],
    source_summary: dict[str, Any],
    enrichment_summary: dict[str, Any] | None,
    notification_stats: dict[str, Any],
    notification_channel: str = "unknown",
    notification_dry_run: bool = False,
    notification_status_override: str = "",
    receipt_verifier: AuthenticatedStrategyReceiptResolver | None = None,
) -> AlphaRunContract:
    decision = dict(review.get("decision") or {})
    diagnostics = dict(review.get("selection_diagnostics") or {})
    watchlist = list(review.get("watchlist") or [])
    source_status = str(source_summary.get("status") or "unknown")
    enrichment = dict(enrichment_summary or {})
    enrichment_status = str(enrichment.get("status") or "not_run")
    coverage = evaluate_premarket_coverage(enrichment)
    data_eligible = coverage.status in {"complete", "partial"}
    persisted_slate = dict(source_summary.get("ranked_research_slate") or {})
    if persisted_slate:
        slate = validate_ranked_research_slate(
            persisted_slate,
            market_date=generated_at[:10],
            production=bool(source_summary.get("require_watcher_proof")),
        )
    else:
        slate = build_ranked_research_slate(
            signals,
            target=5,
            data_eligible=data_eligible,
            generated_at=generated_at,
            market_date=generated_at[:10],
            scan_id=scan_id,
            require_safety=bool(source_summary.get("require_watcher_proof")),
        )
    frozen_publication_rows = source_summary.get("ranked_research_publication_rows")
    derived_publication_rows = apply_publication_semantics(
        list(slate.get("rows") or []),
        slate=slate,
        coverage={"lanes": slate.get("lane_statuses") or {}},
        require_watcher_proof=bool(source_summary.get("require_watcher_proof")),
        receipt_verifier=receipt_verifier,
    )
    if isinstance(frozen_publication_rows, list):
        published_signals = [dict(row) for row in frozen_publication_rows]
        if source_summary.get("require_watcher_proof"):
            validate_frozen_publication_rows(
                published_signals,
                slate=slate,
                market_date=generated_at[:10],
                production=True,
            )
        expected_selection_ids = list(slate.get("selection_ids") or [])
        actual_selection_ids = [
            str(row.get("research_selection_id") or "") for row in published_signals
        ]
        if actual_selection_ids != expected_selection_ids:
            raise ValueError(
                "Run-contract publication rows do not match the immutable slate cohort."
            )
        if published_signals != derived_publication_rows:
            raise ValueError(
                "Run-contract publication rows do not match exact authenticated "
                "frozen-slate publication semantics."
            )
    else:
        # A standalone contract build has no database-backed receipt resolver,
        # so it may reconstruct Tier 1 only. Operational Tier 2/3 survives
        # only when rederived with the cycle's persisted-receipt resolver.
        published_signals = derived_publication_rows
    authoritative_slate = bool(persisted_slate) or isinstance(
        frozen_publication_rows, list
    )
    exact_official_rows = (
        official_publication_rows(
            published_signals,
            slate=slate,
            production=True,
            limit=3,
        )
        if authoritative_slate and source_summary.get("require_watcher_proof")
        else []
    )
    legacy_official_count = (
        len(watchlist)
        if not authoritative_slate
        and not decision.get("no_trade")
        and str(decision.get("decision_tier") or "") == "clean_edge"
        else 0
    )
    official_selected_count = (
        len(exact_official_rows) if authoritative_slate else legacy_official_count
    )
    publication = publication_counts(
        published_signals,
        official_selected=official_selected_count,
    )
    if publication["ranked_research"] != int(slate.get("published_count") or 0):
        raise ValueError(
            "Run-contract publication counts do not match the immutable research slate."
        )
    source_collected = _first_count(
        source_summary.get("source_collected"),
        source_summary.get("rows_normalized"),
        source_summary.get("rows_collected"),
        source_summary.get("candidate_count"),
        source_summary.get("symbols_returned"),
        len(signals),
    )
    primary_verified = _first_count(
        enrichment.get("primary_verified_count"),
        max(
            coverage.verified_count - _first_count(enrichment.get("secondary_fallback_count")),
            0,
        ),
    )
    core = dict(source_summary.get("core_universe") or {})
    if not core:
        core = {
            "contract_status": source_summary.get("core_universe_status"),
            "contract_membership_count": source_summary.get("core_universe_count"),
            "contract_hash_sha256": source_summary.get("core_universe_hash_sha256"),
        }
    lane_counts = dict(source_summary.get("lane_counts") or {})
    slate_lineage = dict(source_summary.get("ranked_research_slate_lineage") or {})
    slate_source_scan_id = str(slate.get("scan_id") or "")
    expected_reuse_status = (
        "CURRENT_SCAN" if slate_source_scan_id == scan_id else "GOVERNED_DAILY_FREEZE_REUSE"
    )
    # A persisted slate from another scan is only valid when the caller has
    # carried the explicit, governed retry lineage.  Do not infer consent
    # from the artifact's scan id or default the missing fields: doing so
    # makes an arbitrary same-day artifact look like the current cohort.
    if slate_source_scan_id != scan_id:
        required_reuse_fields = (
            "schema_version",
            "slate_id",
            "slate_content_hash_sha256",
            "frozen_source_scan_id",
            "current_scan_id",
            "reuse_status",
        )
        if not isinstance(slate_lineage, dict) or any(
            not str(slate_lineage.get(field) or "").strip()
            for field in required_reuse_fields
        ):
            raise ValueError(
                "FROZEN_SLATE_SCAN_MISMATCH: persisted slate scan_id differs "
                "from current scan_id without governed reuse lineage."
            )
    declared_source_scan_id = str(
        slate_lineage.get("frozen_source_scan_id") or slate_source_scan_id
    )
    declared_current_scan_id = str(slate_lineage.get("current_scan_id") or scan_id)
    declared_reuse_status = str(
        slate_lineage.get("reuse_status") or expected_reuse_status
    )
    lineage_identity_matches = (
        str(slate_lineage.get("schema_version") or "")
        == "dawnstrike.luna.frozen_slate_selection_lineage.v1"
        and str(slate_lineage.get("slate_id") or "") == str(slate.get("slate_id") or "")
        and str(slate_lineage.get("slate_content_hash_sha256") or "")
        == str(slate.get("content_hash_sha256") or "")
    )
    if persisted_slate and (
        not lineage_identity_matches
        or declared_source_scan_id != slate_source_scan_id
        or declared_current_scan_id != scan_id
        or declared_reuse_status != expected_reuse_status
    ):
        raise ValueError(
            "FROZEN_SLATE_SCAN_MISMATCH: run-contract frozen slate lineage is inconsistent."
        )
    slate_reuse_status = expected_reuse_status
    enrichment_symbols = tuple(
        sorted(
            {
                str(value).upper().strip()
                for value in enrichment.get("selected_symbols") or []
                if str(value).strip()
            }
        )
    )
    if coverage.selected_count != len(enrichment_symbols):
        raise ValueError(
            "Premarket selected_count must match the explicit research symbol universe."
        )
    lane_statuses = {
        str(name): dict(value)
        for name, value in dict(slate.get("lane_statuses") or {}).items()
        if isinstance(value, dict)
    }
    strategy_contributions = _strategy_contribution_summary(
        signals,
        published_signals,
        source_summary=source_summary,
    )
    if persisted_slate:
        # The frozen slate, not the current mover-enrichment cohort, owns the
        # exact research identity.  This is especially important when a
        # healthy core lane publishes while the mover lane is unavailable or
        # when a governed retry reuses a prior scan's cohort.
        research_symbols = tuple(str(value).upper() for value in slate.get("symbols") or [])
        slate_coverage_status = str(slate.get("coverage_status") or "").strip().upper()
        combined_data_eligible = (
            any(value.get("data_eligible") is True for value in lane_statuses.values())
            if lane_statuses
            else slate_coverage_status
            not in {"", "DATA_UNAVAILABLE", "INELIGIBLE", "BLOCKED"}
        )
        contract_coverage_status = slate_coverage_status.lower()
    else:
        research_symbols = enrichment_symbols
        combined_data_eligible = not coverage.data_ineligible
        contract_coverage_status = coverage.status
    alertable_count = sum(
        1
        for row in signals
        if _truthy(row.get("can_alert")) and not str(row.get("no_trade_reason") or "").strip()
    )
    if source_status not in {"success", "ok"}:
        outcome = SelectionOutcome.SOURCE_FAILED
    elif not combined_data_eligible:
        outcome = SelectionOutcome.DATA_INELIGIBLE
    elif official_selected_count:
        outcome = SelectionOutcome.WATCHLIST_READY
    elif signals and all(_truthy(row.get("fixture_only")) for row in signals):
        outcome = SelectionOutcome.REHEARSAL_COMPLETE
    elif _all_plan_inputs_ineligible(signals):
        outcome = SelectionOutcome.DATA_INELIGIBLE
    else:
        outcome = SelectionOutcome.VALID_NO_EDGE
    primary_veto = (
        str(
            slate.get("slate_shortfall_reason")
            or decision.get("primary_reason_code")
            or diagnostics.get("primary_reason_code")
            or decision.get("reason")
            or ""
        )
        if persisted_slate
        else str(
            coverage.reason_code
            or decision.get("primary_reason_code")
            or diagnostics.get("primary_reason_code")
            or decision.get("reason")
            or ""
        )
    )
    return AlphaRunContract(
        producer="alphaops",
        producer_run_id=scan_id,
        market_date=generated_at[:10],
        model_version=ALPHA_MODEL_VERSION,
        source_status=source_status,
        enrichment_status=enrichment_status,
        ranked_count=ranked_count,
        signal_count=len(signals),
        alertable_count=alertable_count,
        research_candidate_count=len(research_symbols),
        research_symbols=research_symbols,
        premarket_selected_count=coverage.selected_count,
        premarket_verified_count=coverage.verified_count,
        premarket_verified_ratio=coverage.verified_ratio,
        coverage_status=contract_coverage_status,
        selection_outcome=outcome.value,
        primary_veto=primary_veto,
        notification_channel=notification_channel,
        notification_dry_run=notification_dry_run,
        notification_status=_notification_status(
            notification_stats,
            dry_run=notification_dry_run,
            override=notification_status_override,
        ),
        source_collected=source_collected,
        enrichment_selected=coverage.selected_count,
        primary_verified=primary_verified,
        ranked_research=publication["ranked_research"],
        paper_plan_qualified=publication["paper_plan_qualified"],
        alertable_trade=publication["alertable_trade"],
        official_selected=publication["official_selected"],
        slate_shortfall_reason=str(slate.get("slate_shortfall_reason") or ""),
        pre_watcher_alert_gate_count=alertable_count,
        alertable_count_semantics=(
            "legacy_pre_watcher_alert_gate; authoritative_tier3=alertable_trade_count"
        ),
        source_collected_count=source_collected,
        enrichment_selected_count=coverage.selected_count,
        primary_verified_count=primary_verified,
        ranked_research_count=publication["ranked_research"],
        paper_plan_qualified_count=publication["paper_plan_qualified"],
        alertable_trade_count=publication["alertable_trade"],
        official_selected_count=publication["official_selected"],
        core_universe_status=str(
            core.get("contract_status") or core.get("status") or "DATA_UNAVAILABLE"
        ),
        core_universe_count=max(
            int(core.get("contract_membership_count") or core.get("membership_count") or 0), 0
        ),
        core_universe_hash_sha256=str(
            core.get("contract_hash_sha256") or core.get("content_hash_sha256") or ""
        ),
        core_universe_market_date=str(
            core.get("requested_market_date")
            or source_summary.get("market_date")
            or generated_at[:10]
        ),
        core_snapshot_status=str(core.get("status") or "DATA_UNAVAILABLE"),
        core_snapshot_requested_count=max(int(core.get("requested_count") or 0), 0),
        core_snapshot_returned_count=max(int(core.get("returned_count") or 0), 0),
        core_snapshot_eligible_count=max(int(core.get("eligible_count") or 0), 0),
        core_snapshot_fresh_count=max(int(core.get("fresh_count") or 0), 0),
        core_snapshot_fresh_verified_count=max(
            int(core.get("fresh_verified_count") or 0), 0
        ),
        core_snapshot_stale_count=max(int(core.get("stale_count") or 0), 0),
        core_snapshot_missing_count=max(int(core.get("missing_count") or 0), 0),
        core_snapshot_unknown_count=max(int(core.get("unknown_count") or 0), 0),
        core_snapshot_duplicate_count=max(int(core.get("duplicate_count") or 0), 0),
        core_snapshot_coverage_status=str(
            core.get("coverage_status") or core.get("status") or "DATA_UNAVAILABLE"
        ),
        core_snapshot_coverage_receipt_ids=tuple(
            str(item) for item in core.get("coverage_receipt_ids") or []
        ),
        core_snapshot_coverage_receipt_hashes=tuple(
            str(item) for item in core.get("coverage_receipt_hashes") or []
        ),
        core_snapshot_complete=(
            str(core.get("status") or "") == "READY"
            and int(core.get("requested_count") or 0) > 0
            and int(core.get("requested_count") or 0)
            == int(core.get("returned_count") or 0)
        ),
        core_index_verdicts=dict(core.get("index_verdicts") or {}),
        core_raw_artifact_hashes=tuple(
            str(item) for item in core.get("raw_artifact_hashes") or []
        ),
        core_member_set_hash_sha256=str(core.get("canonical_member_set_hash_sha256") or ""),
        lane_counts=lane_counts,
        lane_statuses=lane_statuses,
        slate_id=str(slate.get("slate_id") or ""),
        slate_content_hash_sha256=str(
            slate.get("content_hash_sha256") or ""
        ),
        slate_market_date=str(slate.get("market_date") or generated_at[:10]),
        slate_source_scan_id=slate_source_scan_id,
        slate_reuse_status=slate_reuse_status,
        slate_published_count=max(
            int(slate.get("published_count") or 0), 0
        ),
        slate_selection_ids=tuple(
            str(item) for item in slate.get("selection_ids") or []
        ),
        strategy_contributions=strategy_contributions,
        strategy_adapter_provenance=dict(
            dict(source_summary.get("morning_strategy_adapter") or {}).get("provenance")
            or {}
        ),
    )


def _strategy_contribution_summary(
    signals: list[dict[str, Any]],
    published_signals: list[dict[str, Any]],
    *,
    source_summary: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Summarize candidate attempts separately from the frozen slate cohort."""

    observed: dict[str, dict[str, Any]] = {}
    seen_by_cohort: dict[str, set[tuple[str, str, str, str]]] = {
        "current": set(),
        "slate": set(),
    }

    def add(
        row: dict[str, Any],
        *,
        ticker: str,
        cohort: str,
        adapter: bool = False,
    ) -> None:
        strategy_id = str(row.get("strategy_id") or row.get("decision_strategy_id") or "").strip()
        strategy_version = str(row.get("strategy_version") or "").strip()
        semantics = str(row.get("strategy_semantics_fingerprint") or "").strip()
        source_id = str(
            row.get("source_signal_id")
            or row.get("prior_session_signal_id")
            or row.get("signal_id")
            or row.get("signal_key")
            or ""
        ).strip()
        if not strategy_id:
            return
        key = (strategy_id, strategy_version, semantics, source_id)
        if key in seen_by_cohort[cohort]:
            return
        seen_by_cohort[cohort].add(key)
        item = observed.setdefault(
            strategy_id,
            {
                "strategy_id": strategy_id,
                "current_versions": set(),
                "current_semantics": set(),
                "slate_versions": set(),
                "slate_semantics": set(),
                "current_attempt_candidate_count": 0,
                "slate_count": 0,
                "selected_symbols": set(),
                "receipt_ids": set(),
                "eligible_count": 0,
                "adapter_candidate_count": 0,
                "current_attempt_eligible_count": 0,
                "slate_adapter_candidate_count": 0,
            },
        )
        item[f"{cohort}_versions"].add(strategy_version)
        item[f"{cohort}_semantics"].add(semantics)
        if cohort == "current":
            item["current_attempt_candidate_count"] += 1
            if row.get("research_pick_eligible") is True:
                item["current_attempt_eligible_count"] += 1
            if adapter or str(row.get("strategy_adapter") or ""):
                item["adapter_candidate_count"] += 1
        else:
            item["slate_count"] += 1
            item["selected_symbols"].add(ticker)
            if adapter or str(row.get("strategy_adapter") or ""):
                item["slate_adapter_candidate_count"] += 1
            if row.get("research_pick_eligible") is True:
                item["eligible_count"] += 1
            receipt_id = str(row.get("receipt_id") or "").strip()
            receipt = row.get("strategy_decision_receipt") or row.get("decision_receipt")
            if isinstance(receipt, dict):
                receipt_id = str(receipt.get("receipt_id") or receipt_id).strip()
            if receipt_id:
                item["receipt_ids"].add(receipt_id)

    for row in signals:
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        add(row, ticker=ticker, cohort="current")
        for contributor in row.get("strategy_contributors") or []:
            if isinstance(contributor, dict):
                add(
                    contributor,
                    ticker=ticker,
                    cohort="current",
                    adapter=bool(contributor.get("strategy_adapter")),
                )
    for row in published_signals:
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        add(row, ticker=ticker, cohort="slate")
        for contributor in row.get("strategy_contributors") or []:
            if isinstance(contributor, dict):
                add(
                    contributor,
                    ticker=ticker,
                    cohort="slate",
                    adapter=bool(contributor.get("strategy_adapter")),
                )
    adapter_summary = source_summary.get("morning_strategy_adapter")
    enabled = (
        adapter_summary.get("enabled_strategy_ids")
        if isinstance(adapter_summary, dict)
        else ()
    )
    enabled_identities = (
        adapter_summary.get("enabled_strategy_identities")
        if isinstance(adapter_summary, dict)
        else {}
    )
    if not isinstance(enabled_identities, dict) and isinstance(adapter_summary, dict):
        provenance = adapter_summary.get("provenance")
        enabled_identities = (
            provenance.get("enabled_strategy_identities")
            if isinstance(provenance, dict)
            else {}
        )
    for strategy_id in enabled or ():
        identity = (
            enabled_identities.get(str(strategy_id), {})
            if isinstance(enabled_identities, dict)
            else {}
        )
        item = observed.setdefault(
            str(strategy_id),
            {
                "strategy_id": str(strategy_id),
                "current_versions": set(),
                "current_semantics": set(),
                "slate_versions": set(),
                "slate_semantics": set(),
                "current_attempt_candidate_count": 0,
                "slate_count": 0,
                "selected_symbols": set(),
                "receipt_ids": set(),
                "eligible_count": 0,
                "adapter_candidate_count": 0,
                "current_attempt_eligible_count": 0,
                "slate_adapter_candidate_count": 0,
            },
        )
        if isinstance(identity, dict):
            version = str(identity.get("strategy_version") or "").strip()
            semantics = str(identity.get("strategy_semantics_fingerprint") or "").strip()
            if version:
                item["slate_versions"].add(version)
            if semantics:
                item["slate_semantics"].add(semantics)
    return {
        strategy_id: {
            "strategy_id": strategy_id,
            "strategy_versions": sorted(
                item["slate_versions"] or item["current_versions"]
            ),
            "strategy_semantics_fingerprints": sorted(
                item["slate_semantics"] or item["current_semantics"]
            ),
            "candidate_count": int(item["current_attempt_candidate_count"]),
            "current_attempt_candidate_count": int(item["current_attempt_candidate_count"]),
            "slate_count": int(item["slate_count"]),
            "selected_symbols": sorted(item["selected_symbols"]),
            "receipt_ids": sorted(item["receipt_ids"]),
            "eligible_count": int(item["eligible_count"]),
            "current_attempt_eligible_count": int(item["current_attempt_eligible_count"]),
            "adapter_candidate_count": int(item["adapter_candidate_count"]),
            "slate_adapter_candidate_count": int(item["slate_adapter_candidate_count"]),
            "research_only": True,
            "broker_execution_enabled": False,
        }
        for strategy_id, item in sorted(observed.items())
    }


def _all_plan_inputs_ineligible(signals: list[dict[str, Any]]) -> bool:
    return bool(signals) and all(
        str(row.get("plan_input_status") or "") == "ineligible_missing_truth" for row in signals
    )


def _notification_status(
    stats: dict[str, Any],
    *,
    dry_run: bool,
    override: str,
) -> str:
    if override in {"pending", "delivery_failed"}:
        return override
    if int(stats.get("sent") or 0) > 0:
        return "dry_run_recorded" if dry_run else "delivery_recorded"
    if int(stats.get("skipped") or 0) > 0:
        return "deduplicated"
    return "not_dispatched"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _first_count(*values: Any) -> int:
    """Read the first present count while preserving an explicit zero."""

    for value in values:
        if value is None or value == "":
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 0


__all__ = [
    "AlphaRunContract",
    "SelectionOutcome",
    "build_alpha_run_contract",
]
