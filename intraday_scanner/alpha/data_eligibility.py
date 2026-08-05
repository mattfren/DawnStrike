"""Shared fail-closed market-data coverage rules for AlphaOps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PremarketCoverage:
    status: str
    selected_count: int
    verified_count: int
    verified_ratio: float | None
    fallback_status: str

    @property
    def data_ineligible(self) -> bool:
        return self.status in {"insufficient", "unavailable"}

    @property
    def reason_code(self) -> str:
        if self.status == "unavailable":
            return "premarket_coverage_unavailable"
        if self.status == "insufficient":
            return "premarket_coverage_insufficient"
        return ""

    def operator_reason(self) -> str:
        excluded = max(self.selected_count - self.verified_count, 0)
        return (
            "Premarket data coverage was insufficient: "
            f"{self.verified_count} of {self.selected_count} selected candidates had "
            f"verified bars; {excluded} were excluded."
        )


def evaluate_premarket_coverage(summary: dict[str, Any] | None) -> PremarketCoverage:
    """Classify coverage without turning missing candidates into a market opinion."""

    payload = dict(summary or {})
    nested = payload.get("premarket_enrichment")
    if isinstance(nested, dict):
        payload = dict(nested)
    selected = max(_integer(payload.get("selected_count")), 0)
    verified = max(_integer(payload.get("verified_count")), 0)
    fallback_status = str(payload.get("secondary_fallback_status") or "not_applicable")
    ratio = round(verified / selected, 4) if selected else None
    if selected == 0:
        status = "not_applicable"
    elif verified == 0:
        status = "unavailable"
    elif fallback_status == "ceiling_exceeded_not_applied" and verified < selected:
        status = "insufficient"
    elif verified >= selected:
        status = "complete"
    else:
        status = "partial"
    return PremarketCoverage(
        status=status,
        selected_count=selected,
        verified_count=verified,
        verified_ratio=ratio,
        fallback_status=fallback_status,
    )


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
