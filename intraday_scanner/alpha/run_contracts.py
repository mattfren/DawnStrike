"""Typed operational truth for the authoritative AlphaOps control plane."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION
from intraday_scanner.alpha.data_eligibility import evaluate_premarket_coverage


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


__all__ = [
    "AlphaRunContract",
    "SelectionOutcome",
    "build_alpha_run_contract",
]
