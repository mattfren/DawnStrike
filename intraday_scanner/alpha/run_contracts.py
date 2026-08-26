"""Typed operational truth for the authoritative AlphaOps control plane."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION
from intraday_scanner.alpha.data_eligibility import evaluate_premarket_coverage
from intraday_scanner.services.luna_research_slate_service import (
    apply_publication_semantics,
    build_ranked_research_slate,
    publication_counts,
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
    lane_counts: dict[str, dict[str, int]] = field(default_factory=dict)
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
) -> AlphaRunContract:
    decision = dict(review.get("decision") or {})
    diagnostics = dict(review.get("selection_diagnostics") or {})
    watchlist = list(review.get("watchlist") or [])
    source_status = str(source_summary.get("status") or "unknown")
    enrichment = dict(enrichment_summary or {})
    enrichment_status = str(enrichment.get("status") or "not_run")
    coverage = evaluate_premarket_coverage(enrichment)
    data_eligible = coverage.status in {"complete", "partial"}
    slate = build_ranked_research_slate(
        signals,
        target=5,
        data_eligible=data_eligible,
    )
    published_signals = apply_publication_semantics(
        signals,
        slate=slate,
        coverage=enrichment,
    )
    publication = publication_counts(
        published_signals,
        official_selected=(
            len(watchlist)
            if not decision.get("no_trade")
            and str(decision.get("decision_tier") or "") == "clean_edge"
            else 0
        ),
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
            coverage.verified_count
            - _first_count(enrichment.get("secondary_fallback_count")),
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
    research_symbols = tuple(
        sorted(
            {
                str(value).upper().strip()
                for value in enrichment.get("selected_symbols") or []
                if str(value).strip()
            }
        )
    )
    if coverage.selected_count != len(research_symbols):
        raise ValueError(
            "Premarket selected_count must match the explicit research symbol universe."
        )
    alertable_count = sum(
        1
        for row in signals
        if _truthy(row.get("can_alert")) and not str(row.get("no_trade_reason") or "").strip()
    )
    if source_status not in {"success", "ok"}:
        outcome = SelectionOutcome.SOURCE_FAILED
    elif coverage.data_ineligible:
        outcome = SelectionOutcome.DATA_INELIGIBLE
    elif watchlist:
        outcome = SelectionOutcome.WATCHLIST_READY
    elif signals and all(_truthy(row.get("fixture_only")) for row in signals):
        outcome = SelectionOutcome.REHEARSAL_COMPLETE
    elif _all_plan_inputs_ineligible(signals):
        outcome = SelectionOutcome.DATA_INELIGIBLE
    else:
        outcome = SelectionOutcome.VALID_NO_EDGE
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
        coverage_status=coverage.status,
        selection_outcome=outcome.value,
        primary_veto=str(
            coverage.reason_code
            or decision.get("primary_reason_code")
            or diagnostics.get("primary_reason_code")
            or decision.get("reason")
            or ""
        ),
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
        source_collected_count=source_collected,
        enrichment_selected_count=coverage.selected_count,
        primary_verified_count=primary_verified,
        ranked_research_count=publication["ranked_research"],
        paper_plan_qualified_count=publication["paper_plan_qualified"],
        alertable_trade_count=publication["alertable_trade"],
        official_selected_count=publication["official_selected"],
        core_universe_status=str(core.get("contract_status") or core.get("status") or "DATA_UNAVAILABLE"),
        core_universe_count=max(int(core.get("contract_membership_count") or core.get("membership_count") or 0), 0),
        core_universe_hash_sha256=str(core.get("contract_hash_sha256") or core.get("content_hash_sha256") or ""),
        lane_counts=lane_counts,
    )


def _all_plan_inputs_ineligible(signals: list[dict[str, Any]]) -> bool:
    return bool(signals) and all(
        str(row.get("plan_input_status") or "") == "ineligible_missing_truth"
        for row in signals
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
