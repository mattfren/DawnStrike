"""Independent, fail-closed Luna research slate publication semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
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
    generated_at: str | None = None,
    market_date: str | None = None,
    scan_id: str | None = None,
    canonical_member_ids: Iterable[str] | None = None,
    require_safety: bool = False,
) -> dict[str, Any]:
    """Rank distinct, non-vetoed rows for research observation only.

    A row is never admitted merely to reach ``target``.  In particular stale,
    hard-vetoed, unsafe, fabricated, and missing-truth rows are excluded.
    """

    requested = max(int(target), 0)
    source = [dict(row) for row in (rows or [])]
    safety_blockers: list[str] = []
    if not data_eligible:
        selected: list[dict[str, Any]] = []
    else:
        selected = []
        seen: set[str] = set()
        for row in sorted(source, key=_rank_key, reverse=True):
            ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
            if (
                not ticker
                or ticker in seen
                or not _safe_for_research(row, require_safety=require_safety)
            ):
                if ticker and require_safety:
                    safety_blockers.extend(_safety_blockers(row))
                continue
            seen.add(ticker)
            selected.append(_annotate(row, rank=len(selected) + 1, tier=TIER1))
            if len(selected) >= requested:
                break
    reason = shortfall_reason.strip()
    if len(selected) < requested and not reason:
        reason = (
            "DATA_UNAVAILABLE" if not data_eligible else "fewer than target safe-to-study episodes"
        )
    generated = generated_at or datetime.now(timezone.utc).isoformat()
    input_ids = sorted(
        {
            str(
                row.get("signal_id")
                or row.get("signal_key")
                or row.get("ticker")
                or row.get("symbol")
                or ""
            )
            .strip()
            .upper()
            for row in source
            if str(
                row.get("signal_id")
                or row.get("signal_key")
                or row.get("ticker")
                or row.get("symbol")
                or ""
            ).strip()
        }
    )
    member_ids = sorted(
        {str(item).strip().upper() for item in (canonical_member_ids or []) if str(item).strip()}
    )
    payload = {
        "schema_version": "dawnstrike.luna.ranked_research_slate.v1",
        "generated_at": generated,
        "market_date": (market_date or generated[:10])[:10],
        "scan_id": str(scan_id or ""),
        "canonical_input_ids": input_ids,
        "canonical_member_ids": member_ids,
        "target_count": requested,
        "published_count": len(selected),
        "ranked_research_count": len(selected),
        "slate_shortfall_reason": reason if len(selected) < requested else "",
        "safety_blockers": sorted(set(safety_blockers)) if require_safety else [],
        "rows": selected,
        "symbols": [str(row["ticker"]) for row in selected],
        "selection_ids": [str(row["research_selection_id"]) for row in selected],
        "publication_tier": TIER1 if selected else None,
        "research_only": True,
        "broker_execution": "disabled",
        "missing_truth_is_zero": False,
    }
    payload["content_hash_sha256"] = _slate_content_hash(payload)
    payload["slate_id"] = "luna-slate-" + payload["content_hash_sha256"][:24]
    return payload


def apply_publication_semantics(
    rows: Iterable[dict[str, Any]] | None,
    *,
    slate: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
    require_watcher_proof: bool = False,
) -> list[dict[str, Any]]:
    """Annotate rows with Tier 1/2/3 fields without changing legacy classification."""

    source = [dict(row) for row in (rows or [])]
    slate_rows = list((slate or {}).get("rows") or [])
    slate_by_symbol = {str(row.get("ticker") or "").upper(): row for row in slate_rows}
    slate_symbols = set(slate_by_symbol)
    coverage_payload = dict(coverage or {})
    ceiling_block = str(coverage_payload.get("secondary_fallback_status") or "").lower() in {
        "research_only_applied_above_ceiling",
        "applied_research_only_above_ceiling",
        "ceiling_exceeded_not_applied",
    }
    output: list[dict[str, Any]] = []
    for row in source:
        ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
        enriched = dict(row)
        if ticker in slate_symbols and _safe_for_research(
            row, require_safety=require_watcher_proof
        ):
            slate_row = slate_by_symbol[ticker]
            enriched["research_rank"] = slate_row.get("research_rank")
            enriched["research_selection_id"] = slate_row.get("research_selection_id")
            enriched["publication_tier"] = TIER1
            enriched["entry_state"] = "RESEARCH_ONLY"
            row_ceiling_block = (
                ceiling_block
                or _truthy(row.get("research_only_above_ceiling"))
                or _truthy(row.get("above_ceiling"))
                or str(row.get("secondary_fallback_status") or "").lower()
                in {
                    "research_only_applied_above_ceiling",
                    "applied_research_only_above_ceiling",
                    "ceiling_exceeded_not_applied",
                }
            )
            qualified = _plan_qualified(row) and not row_ceiling_block
            enriched["plan_qualification_status"] = (
                "QUALIFIED" if qualified else "WAITING_CURRENT_CHECKS"
            )
            if qualified:
                enriched["publication_tier"] = TIER2
            alertable = (
                qualified
                and _alertable(row, require_watcher_proof=require_watcher_proof)
                and not row_ceiling_block
            )
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


def publication_counts(
    rows: Iterable[dict[str, Any]] | None, *, official_selected: int = 0
) -> dict[str, int]:
    values = list(rows or [])
    return {
        "ranked_research": sum(
            1 for row in values if row.get("publication_tier") in {TIER1, TIER2, TIER3}
        ),
        "paper_plan_qualified": sum(
            1 for row in values if row.get("publication_tier") in {TIER2, TIER3}
        ),
        "alertable_trade": sum(1 for row in values if row.get("publication_tier") == TIER3),
        "official_selected": max(int(official_selected or 0), 0),
    }


def persist_ranked_research_slate(slate: dict[str, Any], output_path: str | Path) -> Path:
    """Persist the exact slate artifact; no database or broker side effects."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(slate, indent=2, sort_keys=True) + "\n"
    if not path.exists():
        path.write_text(serialized, encoding="utf-8")
    return path


def validate_ranked_research_slate(
    slate: dict[str, Any], *, market_date: str | None = None
) -> dict[str, Any]:
    """Verify a frozen slate before it is reused by a retry or monitor."""

    if slate.get("schema_version") != "dawnstrike.luna.ranked_research_slate.v1":
        raise ValueError("ranked research slate schema is invalid")
    if market_date and str(slate.get("market_date") or "") != str(market_date)[:10]:
        raise ValueError("ranked research slate market date is invalid")
    if slate.get("research_only") is not True or slate.get("broker_execution") != "disabled":
        raise ValueError("ranked research slate execution flags are invalid")
    rows = slate.get("rows")
    if not isinstance(rows, list):
        raise ValueError("ranked research slate rows are invalid")
    if int(slate.get("published_count") or -1) != len(rows):
        raise ValueError("ranked research slate count is invalid")
    symbols = [str(row.get("ticker") or "").upper() for row in rows]
    if symbols != list(slate.get("symbols") or []) or len(set(symbols)) != len(symbols):
        raise ValueError("ranked research slate symbols are inconsistent")
    selection_ids = [str(row.get("research_selection_id") or "") for row in rows]
    if selection_ids != list(slate.get("selection_ids") or []) or len(set(selection_ids)) != len(
        selection_ids
    ):
        raise ValueError("ranked research slate selection IDs are inconsistent")
    for row in rows:
        row_hash = str(row.get("research_row_hash_sha256") or "")
        row_payload = {
            key: value for key, value in row.items() if key != "research_row_hash_sha256"
        }
        expected = hashlib.sha256(
            json.dumps(
                row_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode()
        ).hexdigest()
        if row_hash != expected:
            raise ValueError("ranked research slate row hash is invalid")
    content_hash = str(slate.get("content_hash_sha256") or "")
    if content_hash != _slate_content_hash(slate):
        raise ValueError("ranked research slate content hash is invalid")
    if str(slate.get("slate_id") or "") != "luna-slate-" + content_hash[:24]:
        raise ValueError("ranked research slate identity is invalid")
    return slate


def _safe_for_research(row: dict[str, Any], *, require_safety: bool = False) -> bool:
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
    if str(row.get("plan_input_status") or "").lower() in {
        "ineligible_missing_truth",
        "stale",
        "ineligible",
    }:
        return False
    for key in ("hard_avoid_reasons", "hard_veto_reasons", "hard_no_trade_reason"):
        value = row.get(key)
        if isinstance(value, (list, tuple, set)) and any(str(item).strip() for item in value):
            return False
        if isinstance(value, str) and value.strip():
            return False
    if require_safety and _safety_blockers(row):
        return False
    return True


def _safety_blockers(row: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    source_count = (
        row.get("source_count")
        or row.get("source_count_verified")
        or row.get("verified_source_count")
    )
    source_status = str(row.get("source_quality_status") or row.get("source_status") or "").upper()
    if not (
        (isinstance(source_count, (int, float)) and source_count > 0)
        or source_status in {"VERIFIED", "LIMITED", "PASS", "CLEAR"}
    ):
        blockers.append("source_evidence_missing_or_nonpositive")
    freshness = str(
        row.get("freshness_status")
        or row.get("freshness_verdict")
        or row.get("data_freshness")
        or ""
    ).upper()
    if freshness not in {"FRESH", "VERIFIED", "CURRENT", "PASS", "LIMITED"}:
        blockers.append("freshness_missing_or_not_current")
    for field in ("halt_status", "sec_risk_status", "corporate_action_status"):
        value = str(row.get(field) or "").upper()
        if value not in {"CLEAR", "PASS", "VERIFIED", "NONE", "NO_RISK"}:
            blockers.append(f"{field}_not_clear")
    input_status = str(row.get("input_status") or row.get("plan_input_status") or "").upper()
    evidence_status = str(
        row.get("evidence_status") or row.get("safety_evidence_status") or ""
    ).upper()
    if input_status in {
        "UNKNOWN",
        "MISSING",
        "INELIGIBLE",
        "INELIGIBLE_MISSING_TRUTH",
    } or evidence_status in {"UNKNOWN", "MISSING", "INCOMPLETE"}:
        blockers.append("input_or_evidence_status_unknown")
    return blockers


def _plan_qualified(row: dict[str, Any]) -> bool:
    # Alpha's strict public market-structure validator is integrated on the
    # adjacent lane.  Luna deliberately exposes the hook but cannot infer
    # Tier 2/3 from a self-asserted structural plan in this branch.
    market_plan = row.get("alphaops_market_structure_plan")
    if not isinstance(market_plan, dict):
        return False
    try:
        from intraday_scanner.alpha.plan_constructor import (
            validate_alphaops_v5_plan,
        )
    except ImportError:
        return False
    try:
        validated = validate_alphaops_v5_plan(market_plan)
    except (TypeError, ValueError):
        try:
            validated = validate_alphaops_v5_plan(market_plan, row)
        except (TypeError, ValueError):
            return False
    if validated is False:
        return False
    plan_hash = str(
        market_plan.get("plan_hash_sha256") or market_plan.get("strategy_plan_hash_sha256") or ""
    ).lower()
    row_plan_hash = str(
        row.get("strategy_plan_hash_sha256") or row.get("plan_hash_sha256") or ""
    ).lower()
    if len(plan_hash) != 64 or plan_hash != row_plan_hash:
        return False
    for row_key, plan_keys in {
        "entry_trigger": ("entry_trigger", "entry"),
        "invalidation": ("invalidation", "stop", "invalidation_level"),
        "target_1": ("target_1", "target", "first_target"),
    }.items():
        plan_value = next(
            (market_plan.get(key) for key in plan_keys if market_plan.get(key) is not None), None
        )
        if plan_value is not None and _number(row.get(row_key)) != _number(plan_value):
            return False
    plan_direction = str(market_plan.get("direction") or "").upper()
    if plan_direction and plan_direction != str(row.get("direction") or "LONG").upper():
        return False
    if not _static_hard_gates(row) or not _supported_strategy(row):
        return False
    receipt = str(
        row.get("strategy_receipt_status")
        or row.get("decision_receipt_status")
        or row.get("receipt_status")
        or row.get("strategy_receipt_construction_status")
        or ""
    ).upper()
    if receipt != "COMPLETE" or not _immutable_plan_provenance(row):
        return False
    entry = _number(row.get("entry_trigger") or row.get("entry"))
    stop = _number(row.get("invalidation") or row.get("stop") or row.get("invalidation_level"))
    target = _number(row.get("target_1") or row.get("target") or row.get("first_target"))
    direction = str(row.get("direction") or row.get("trade_direction") or "LONG").upper()
    valid_geometry = (
        entry is not None
        and stop is not None
        and target is not None
        and (
            (direction in {"LONG", "BUY"} and entry > stop > 0 and target > entry)
            or (direction in {"SHORT", "SELL"} and stop > entry > 0 and target < entry)
        )
    )
    if not valid_geometry:
        return False
    if abs(entry - stop) / entry > 0.15:
        return False
    after_cost_rr = _number(
        row.get("after_cost_reward_risk_ratio")
        or row.get("reward_risk_ratio_after_cost")
        or row.get("after_cost_rr")
    )
    return (
        row.get("strategy_receipt_paper_entry_eligible") is True
        and after_cost_rr is not None
        and after_cost_rr >= 1.5
    )


def _alertable(row: dict[str, Any], *, require_watcher_proof: bool = False) -> bool:
    return (
        bool(row.get("can_alert"))
        and (not require_watcher_proof or _watcher_current(row))
        and str(row.get("alert_gate_status") or "").upper() in {"PASS", "ALERT_OK"}
        and _static_hard_gates(row)
    )


def _watcher_current(row: dict[str, Any]) -> bool:
    proof = row.get("watcher_current_proof")
    if not isinstance(proof, dict):
        return False
    digest = str(proof.get("proof_hash_sha256") or "").lower()
    signal_id = str(row.get("signal_id") or row.get("signal_key") or "").strip()
    plan = row.get("alphaops_market_structure_plan")
    plan_hash = str(plan.get("plan_hash_sha256") or "") if isinstance(plan, dict) else ""
    if not signal_id or not plan_hash or str(proof.get("signal_id") or "") != signal_id:
        return False
    if str(proof.get("plan_hash_sha256") or "") != plan_hash:
        return False
    checked_at = _parse_watcher_time(proof.get("checked_at"))
    if checked_at is None or abs((datetime.now(timezone.utc) - checked_at).total_seconds()) > 900:
        return False
    row_market_date = str(row.get("market_date") or row.get("generated_at") or "")[:10]
    if row_market_date and checked_at.date().isoformat() != row_market_date:
        return False
    for receipt_key, hash_key in (
        ("quote_receipt", "quote_hash_sha256"),
        ("portfolio_receipt", "portfolio_hash_sha256"),
    ):
        receipt = proof.get(receipt_key)
        receipt_hash = str(proof.get(hash_key) or "").lower()
        if not isinstance(receipt, dict) or not _valid_hash(receipt_hash):
            return False
        expected = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if receipt_hash != expected:
            return False
    canonical = {key: value for key, value in proof.items() if key != "proof_hash_sha256"}
    expected_proof_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        str(proof.get("schema_version") or "") == "alphaops.watcher_current.v1"
        and str(proof.get("status") or "").upper() == "CURRENT"
        and _valid_hash(digest)
        and digest == expected_proof_hash
        and proof.get("research_only") is True
        and proof.get("broker_execution") == "disabled"
    )


def _parse_watcher_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _static_hard_gates(row: dict[str, Any]) -> bool:
    for key in ("hard_avoid_reasons", "hard_veto_reasons", "hard_no_trade_reason"):
        value = row.get(key)
        if (isinstance(value, (list, tuple, set)) and any(str(item).strip() for item in value)) or (
            isinstance(value, str) and value.strip()
        ):
            return False
    for key in (
        "stale",
        "stale_data_flag",
        "fabricated",
        "is_fabricated",
        "synthetic",
        "is_synthetic",
        "unsafe",
        "unsafe_for_research",
    ):
        if _truthy(row.get(key)):
            return False
    for key in ("current_halt", "halted", "recent_offering", "reverse_split_90d"):
        if _truthy(row.get(key)):
            return False
    for key in (
        "halt_status",
        "sec_risk_status",
        "corporate_action_status",
        "source_quality_status",
    ):
        value = str(row.get(key) or "").upper()
        if value in {"FAIL", "FAILED", "BLOCKED", "UNKNOWN", "NOT_VERIFIED", "HALTED", "RISK"}:
            return False
    return True


def _supported_strategy(row: dict[str, Any]) -> bool:
    strategy = str(row.get("strategy_id") or row.get("strategy_version") or "").lower()
    return strategy in {
        "alphaops_v4",
        "alphaops_v5",
        "alphaops_v6_shadow",
        "dawnstrike-alphaops-v6-shadow",
    } or strategy.startswith("dawnstrike-alphaops")


def _immutable_plan_provenance(row: dict[str, Any]) -> bool:
    contract = (
        row.get("structural_plan_contract")
        or row.get("alphaops_plan_contract")
        or row.get("plan_contract")
    )
    if not isinstance(contract, dict) or str(contract.get("status") or "").upper() != "COMPLETE":
        return False
    plan_hash = str(contract.get("plan_hash_sha256") or "").lower()
    if len(plan_hash) != 64 or any(char not in "0123456789abcdef" for char in plan_hash):
        return False
    canonical = {key: value for key, value in contract.items() if key != "plan_hash_sha256"}
    expected_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    if expected_hash != plan_hash:
        return False
    provenance = contract.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("independent") is not True:
        return False
    observations = provenance.get("observations")
    if not isinstance(observations, list) or len(observations) < 3:
        return False
    distinct = {
        (str(item.get("source_id") or ""), str(item.get("observation_hash") or ""))
        for item in observations
        if isinstance(item, dict)
    }
    return len(distinct) >= 3 and all(source and digest for source, digest in distinct)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _annotate(row: dict[str, Any], *, rank: int, tier: str) -> dict[str, Any]:
    output = dict(row)
    output["ticker"] = str(output.get("ticker") or output.get("symbol") or "").upper()
    output["research_rank"] = rank
    output["research_selection_id"] = str(
        output.get("signal_id")
        or output.get("signal_key")
        or f"luna-research:{output['ticker']}:{rank}"
    )
    output["publication_tier"] = tier
    output["plan_qualification_status"] = "WAITING_CURRENT_CHECKS"
    output["entry_state"] = "RESEARCH_ONLY"
    output["research_only"] = True
    output["broker_execution"] = "disabled"
    output["research_row_hash_sha256"] = hashlib.sha256(
        json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    return output


def _rank_key(row: dict[str, Any]) -> tuple[float, float, str]:
    def number(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("-inf")

    return (
        number(row.get("alpha_score")),
        number(row.get("score") or row.get("total_score")),
        str(row.get("ticker") or row.get("symbol") or ""),
    )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _slate_content_hash(slate: dict[str, Any]) -> str:
    payload = {
        key: value for key, value in slate.items() if key not in {"content_hash_sha256", "slate_id"}
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


__all__ = [
    "TIER1",
    "TIER2",
    "TIER2_WAITING",
    "TIER3",
    "apply_publication_semantics",
    "build_ranked_research_slate",
    "persist_ranked_research_slate",
    "publication_counts",
    "validate_ranked_research_slate",
]
