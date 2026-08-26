"""Independent, fail-closed Luna research slate publication semantics."""

from __future__ import annotations

from collections.abc import Iterable
import json
from pathlib import Path
from typing import Any

TIER1 = "RANKED_RESEARCH_CANDIDATE"
TIER2 = "PAPER_PLAN_QUALIFIED"
TIER2_WAITING = "WAITING_CURRENT_CHECKS"
TIER3 = "ALERTABLE_PAPER_ENTRY"


def build_ranked_research_slate(
    rows: Iterable[dict[str, Any]] | None,
    *,
    target: int = 5,
    data_eligible: bool = True,
    shortfall_reason: str = "",
) -> dict[str, Any]:
    """Rank distinct, non-vetoed rows for research observation only.

    A row is never admitted merely to reach ``target``.  In particular stale,
    hard-vetoed, unsafe, fabricated, and missing-truth rows are excluded.
    """

    requested = max(int(target), 0)
    source = [dict(row) for row in (rows or [])]
    if not data_eligible:
        selected: list[dict[str, Any]] = []
    else:
        selected = []
        seen: set[str] = set()
        for row in sorted(source, key=_rank_key, reverse=True):
            ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
            if not ticker or ticker in seen or not _safe_for_research(row):
                continue
            seen.add(ticker)
            selected.append(_annotate(row, rank=len(selected) + 1, tier=TIER1))
            if len(selected) >= requested:
                break
    reason = shortfall_reason.strip()
    if len(selected) < requested and not reason:
        reason = "DATA_UNAVAILABLE" if not data_eligible else "fewer than target safe-to-study episodes"
    return {
        "schema_version": "dawnstrike.luna.ranked_research_slate.v1",
        "target_count": requested,
        "published_count": len(selected),
        "slate_shortfall_reason": reason if len(selected) < requested else "",
        "rows": selected,
        "symbols": [str(row["ticker"]) for row in selected],
        "publication_tier": TIER1 if selected else None,
        "research_only": True,
        "broker_execution": "disabled",
        "missing_truth_is_zero": False,
    }


def apply_publication_semantics(
    rows: Iterable[dict[str, Any]] | None,
    *,
    slate: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Annotate rows with Tier 1/2/3 fields without changing legacy classification."""

    source = [dict(row) for row in (rows or [])]
    slate_rows = list((slate or {}).get("rows") or [])
    slate_symbols = {str(row.get("ticker") or "").upper() for row in slate_rows}
    coverage_payload = dict(coverage or {})
    ceiling_block = str(coverage_payload.get("secondary_fallback_status") or "").lower() in {
        "research_only_applied_above_ceiling",
        "ceiling_exceeded_not_applied",
    }
    output: list[dict[str, Any]] = []
    for row in source:
        ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
        enriched = dict(row)
        if ticker in slate_symbols and _safe_for_research(row):
            enriched["publication_tier"] = TIER1
            enriched["entry_state"] = "RESEARCH_ONLY"
            qualified = _plan_qualified(row) and not ceiling_block
            enriched["plan_qualification_status"] = "QUALIFIED" if qualified else "WAITING_CURRENT_CHECKS"
            if qualified:
                enriched["publication_tier"] = TIER2
            alertable = qualified and _alertable(row) and not ceiling_block
            if alertable:
                enriched["publication_tier"] = TIER3
                enriched["entry_state"] = "ALERTABLE_PAPER_ENTRY"
            elif qualified:
                enriched["entry_state"] = "PAPER_PLAN_QUALIFIED"
        else:
            enriched.setdefault("publication_tier", None)
            enriched.setdefault("plan_qualification_status", "NOT_SELECTED")
            enriched.setdefault("entry_state", "NOT_PUBLISHED")
        output.append(enriched)
    return output


def publication_counts(rows: Iterable[dict[str, Any]] | None, *, official_selected: int = 0) -> dict[str, int]:
    values = list(rows or [])
    return {
        "ranked_research": sum(1 for row in values if row.get("publication_tier") in {TIER1, TIER2, TIER3}),
        "paper_plan_qualified": sum(1 for row in values if row.get("publication_tier") in {TIER2, TIER3}),
        "alertable_trade": sum(1 for row in values if row.get("publication_tier") == TIER3),
        "official_selected": max(int(official_selected or 0), 0),
    }


def persist_ranked_research_slate(slate: dict[str, Any], output_path: str | Path) -> Path:
    """Persist the exact slate artifact; no database or broker side effects."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(slate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _safe_for_research(row: dict[str, Any]) -> bool:
    ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
    if not ticker or ticker == "NO_TRADE":
        return False
    for key in (
        "fabricated",
        "is_fabricated",
        "synthetic",
        "is_synthetic",
        "fixture_only",
        "unsafe",
        "unsafe_for_research",
        "hard_veto",
        "stale",
        "stale_data_flag",
    ):
        if _truthy(row.get(key)):
            return False
    if str(row.get("plan_input_status") or "").lower() in {"ineligible_missing_truth", "stale", "ineligible"}:
        return False
    for key in ("hard_avoid_reasons", "hard_veto_reasons", "no_trade_reason"):
        value = row.get(key)
        if isinstance(value, (list, tuple, set)) and any(str(item).strip() for item in value):
            return False
        if isinstance(value, str) and value.strip():
            return False
    return True


def _plan_qualified(row: dict[str, Any]) -> bool:
    if row.get("plan_qualified") is True or str(row.get("plan_qualification_status") or "").upper() in {"QUALIFIED", "PASS", "PAPER_PLAN_QUALIFIED"}:
        return True
    # Existing Alpha rows only become Tier 2 after their explicit current-check
    # fields say so; absent fields remain waiting rather than inferred.
    return str(row.get("current_checks_status") or "").upper() in {"PASS", "CURRENT", "VERIFIED"}


def _alertable(row: dict[str, Any]) -> bool:
    return bool(row.get("can_alert")) and str(row.get("alert_gate_status") or "").upper() in {"PASS", "ALERT_OK"}


def _annotate(row: dict[str, Any], *, rank: int, tier: str) -> dict[str, Any]:
    output = dict(row)
    output["ticker"] = str(output.get("ticker") or output.get("symbol") or "").upper()
    output["research_rank"] = rank
    output["publication_tier"] = tier
    output["plan_qualification_status"] = "WAITING_CURRENT_CHECKS"
    output["entry_state"] = "RESEARCH_ONLY"
    output["research_only"] = True
    output["broker_execution"] = "disabled"
    return output


def _rank_key(row: dict[str, Any]) -> tuple[float, float, str]:
    def number(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("-inf")
    return (number(row.get("alpha_score")), number(row.get("score") or row.get("total_score")), str(row.get("ticker") or row.get("symbol") or ""))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


__all__ = ["TIER1", "TIER2", "TIER2_WAITING", "TIER3", "apply_publication_semantics", "build_ranked_research_slate", "persist_ranked_research_slate", "publication_counts"]
