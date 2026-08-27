"""Independent, fail-closed Luna research slate publication semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.alpha.v5_policy import ALPHAOPS_V5_ACCOUNT_ID, DEFAULT_V5_POLICY

TIER1 = "RANKED_RESEARCH_CANDIDATE"
TIER2 = "PAPER_PLAN_QUALIFIED"
TIER2_WAITING = "WAITING_CURRENT_CHECKS"
TIER3 = "ALERTABLE_PAPER_ENTRY"
WAITING_EXECUTION_COSTS = "WAITING_EXECUTION_COSTS"
EASTERN = ZoneInfo("America/New_York")


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
    coverage_status: str = "",
    lane_statuses: dict[str, Any] | None = None,
    coverage_limitations: Iterable[str] | None = None,
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
                or not _row_lane_eligible(row, lane_statuses)
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
        "scan_id": str(scan_id or f"luna-research:{(market_date or generated[:10])[:10]}"),
        "canonical_input_ids": input_ids,
        "canonical_member_ids": member_ids,
        "coverage_status": coverage_status.strip().upper()
        or ("ELIGIBLE" if data_eligible else "DATA_UNAVAILABLE"),
        "lane_statuses": dict(lane_statuses or {}),
        "coverage_limitations": sorted(
            {str(item).strip() for item in (coverage_limitations or []) if str(item).strip()}
        ),
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
    receipt_verifier: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Annotate rows with Tier 1/2/3 fields without changing legacy classification."""

    source = [dict(row) for row in (rows or [])]
    slate_rows = list((slate or {}).get("rows") or [])
    slate_by_symbol = {str(row.get("ticker") or "").upper(): row for row in slate_rows}
    slate_symbols = set(slate_by_symbol)
    coverage_payload = dict(coverage or {})
    output: list[dict[str, Any]] = []
    for row in source:
        ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
        enriched = dict(row)
        if ticker in slate_symbols and _safe_for_research(
            row, require_safety=require_watcher_proof
        ):
            slate_row = slate_by_symbol[ticker]
            enriched["research_only"] = True
            enriched["broker_execution"] = "disabled"
            enriched["broker_execution_enabled"] = False
            enriched["research_rank"] = slate_row.get("research_rank")
            enriched["research_selection_id"] = slate_row.get("research_selection_id")
            enriched["publication_tier"] = TIER1
            enriched["entry_state"] = "RESEARCH_ONLY"
            row_ceiling_block = _row_promotion_limited(row, coverage_payload)
            qualified = (
                _plan_qualified(row, receipt_verifier=receipt_verifier)
                and not row_ceiling_block
            )
            enriched["execution_cost_status"] = (
                "READY"
                if _valid_modeled_cost_receipt(
                    row, str(row.get("plan_hash_sha256") or "")
                )
                else WAITING_EXECUTION_COSTS
            )
            # Preserve the broader publication status vocabulary while making
            # the missing-cost reason explicit in its own immutable field.
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


def official_publication_rows(
    rows: Iterable[dict[str, Any]] | None,
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return the exact frozen Tier 2/3 rows represented as official plans.

    The daily research slate is the authoritative cohort. A retry-time review
    watchlist is intentionally not an input here: it cannot add a symbol that
    was absent from the immutable slate or revive an empty frozen cohort.
    """

    selected: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()
    for source in rows or []:
        row = dict(source)
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        if (
            not ticker
            or ticker in seen_tickers
            or row.get("publication_tier") not in {TIER2, TIER3}
            or str(row.get("alert_gate_status") or "").upper() not in {"PASS", "ALERT_OK"}
            or row.get("manual_confirmation_required") is not False
        ):
            continue
        selected.append(row)
        seen_tickers.add(ticker)
        if len(selected) >= max(int(limit), 0):
            break
    return selected


def persist_ranked_research_slate(slate: dict[str, Any], output_path: str | Path) -> Path:
    """Persist the exact slate artifact; no database or broker side effects."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_ranked_research_slate(slate, market_date=str(slate.get("market_date") or ""))
    serialized = json.dumps(slate, indent=2, sort_keys=True) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("existing ranked research slate is unreadable") from exc
        if not isinstance(existing, dict):
            raise ValueError("existing ranked research slate is not an object")
        validate_ranked_research_slate(
            existing,
            market_date=str(slate.get("market_date") or ""),
        )
        return path
    path.write_text(serialized, encoding="utf-8")
    return path


def validate_ranked_research_slate(
    slate: dict[str, Any], *, market_date: str | None = None
) -> dict[str, Any]:
    """Verify a frozen slate before it is reused by a retry or monitor."""

    if slate.get("schema_version") != "dawnstrike.luna.ranked_research_slate.v1":
        raise ValueError("ranked research slate schema is invalid")
    if not str(slate.get("scan_id") or "").strip():
        raise ValueError("ranked research slate producer scan ID is invalid")
    generated_at = _parse_watcher_time(slate.get("generated_at"))
    slate_market_date = str(slate.get("market_date") or "")
    if (
        generated_at is None
        or len(slate_market_date) != 10
        or generated_at.date().isoformat() != slate_market_date
    ):
        raise ValueError("ranked research slate timestamps are invalid")
    if market_date and str(slate.get("market_date") or "") != str(market_date)[:10]:
        raise ValueError("ranked research slate market date is invalid")
    if slate.get("research_only") is not True or slate.get("broker_execution") != "disabled":
        raise ValueError("ranked research slate execution flags are invalid")
    if not str(slate.get("coverage_status") or "").strip() or not isinstance(
        slate.get("lane_statuses"), dict
    ):
        raise ValueError("ranked research slate coverage contract is invalid")
    if not isinstance(slate.get("coverage_limitations"), list):
        raise ValueError("ranked research slate coverage limitations are invalid")
    rows = slate.get("rows")
    if not isinstance(rows, list):
        raise ValueError("ranked research slate rows are invalid")
    try:
        published_count = int(slate.get("published_count"))
        ranked_count = int(slate.get("ranked_research_count"))
        target_count = int(slate.get("target_count"))
    except (TypeError, ValueError) as exc:
        raise ValueError("ranked research slate counts are invalid") from exc
    if (
        published_count != len(rows)
        or ranked_count != len(rows)
        or target_count < 0
        or published_count < 0
        or published_count > target_count
    ):
        raise ValueError("ranked research slate count is invalid")
    symbols = [str(row.get("ticker") or "").upper() for row in rows]
    if symbols != list(slate.get("symbols") or []) or len(set(symbols)) != len(symbols):
        raise ValueError("ranked research slate symbols are inconsistent")
    selection_ids = [str(row.get("research_selection_id") or "") for row in rows]
    if selection_ids != list(slate.get("selection_ids") or []) or len(set(selection_ids)) != len(
        selection_ids
    ):
        raise ValueError("ranked research slate selection IDs are inconsistent")
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError("ranked research slate row is not an object")
        if not _row_lane_eligible(row, slate.get("lane_statuses")):
            raise ValueError("ranked research slate row lane is not eligible")
        if (
            row.get("research_only") is not True
            or row.get("broker_execution") != "disabled"
            or row.get("publication_tier") != TIER1
            or row.get("entry_state") != "RESEARCH_ONLY"
            or int(row.get("research_rank") or 0) != rank
        ):
            raise ValueError("ranked research slate row semantics are invalid")
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


def validated_frozen_selection_signal(
    selection: dict[str, Any],
    *,
    market_date: str,
    allowed_cohorts: Iterable[str] = ("research_radar", "official_telegram"),
) -> dict[str, Any] | None:
    """Resolve one exact cross-scan selection from its immutable slate lineage."""

    payload = selection.get("payload_json")
    if not isinstance(payload, dict):
        return None
    slate = payload.get("frozen_ranked_research_slate")
    lineage = payload.get("frozen_slate_lineage")
    frozen_signal = payload.get("signal")
    if not isinstance(slate, dict) or not isinstance(lineage, dict) or not isinstance(
        frozen_signal, dict
    ):
        return None
    try:
        validate_ranked_research_slate(slate, market_date=market_date)
    except (TypeError, ValueError):
        return None
    allowed = {str(value) for value in allowed_cohorts}
    frozen_scan_id = str(slate.get("scan_id") or "")
    selection_scan_id = str(selection.get("scan_id") or "")
    source_scan_id = str(
        selection.get("source_scan_id") or payload.get("source_scan_id") or ""
    )
    scan_lineage_status = str(
        selection.get("scan_lineage_status")
        or payload.get("scan_lineage_status")
        or ""
    )
    expected_status = (
        "CURRENT_SCAN"
        if frozen_scan_id == selection_scan_id
        else "GOVERNED_DAILY_FREEZE_REUSE"
    )
    frozen_selection_id = str(frozen_signal.get("research_selection_id") or "")
    matching_rows = [
        row
        for row in slate.get("rows") or []
        if str(row.get("research_selection_id") or "") == frozen_selection_id
    ]
    signal_id = str(selection.get("signal_id") or "")
    frozen_signal_id = str(
        frozen_signal.get("signal_id") or frozen_signal.get("signal_key") or ""
    )
    if (
        str(selection.get("cohort") or "") not in allowed
        or str(lineage.get("schema_version") or "")
        != "dawnstrike.luna.frozen_slate_selection_lineage.v1"
        or str(lineage.get("slate_id") or "") != str(slate.get("slate_id") or "")
        or str(lineage.get("slate_content_hash_sha256") or "")
        != str(slate.get("content_hash_sha256") or "")
        or str(lineage.get("frozen_source_scan_id") or "") != frozen_scan_id
        or str(lineage.get("current_scan_id") or "") != selection_scan_id
        or str(lineage.get("reuse_status") or "") != expected_status
        or source_scan_id != frozen_scan_id
        or scan_lineage_status != expected_status
        or not frozen_selection_id
        or len(matching_rows) != 1
        or json.dumps(matching_rows[0], sort_keys=True, separators=(",", ":"))
        != json.dumps(frozen_signal, sort_keys=True, separators=(",", ":"))
        or not signal_id
        or signal_id != frozen_signal_id
        or str(selection.get("ticker") or "").upper()
        != str(frozen_signal.get("ticker") or "").upper()
    ):
        return None
    return dict(frozen_signal)


def _safe_for_research(row: dict[str, Any], *, require_safety: bool = False) -> bool:
    ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
    if not ticker or ticker == "NO_TRADE":
        return False
    if (
        _truthy(row.get("broker_execution_enabled"))
        or (
            str(row.get("broker_execution") or "").strip().lower()
            not in {"", "disabled"}
        )
        or row.get("research_only") is False
    ):
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


def _row_promotion_limited(row: dict[str, Any], coverage: dict[str, Any]) -> bool:
    blocked_statuses = {
        "research_only_applied_above_ceiling",
        "applied_research_only_above_ceiling",
        "ceiling_exceeded_not_applied",
    }
    if (
        _truthy(row.get("research_only_above_ceiling"))
        or _truthy(row.get("above_ceiling"))
        or str(row.get("secondary_fallback_status") or "").lower() in blocked_statuses
    ):
        return True
    lane_payloads = coverage.get("lanes")
    if isinstance(lane_payloads, dict):
        evidence_lane = str(
            row.get("evidence_lane") or row.get("universe_lane") or "mover"
        ).lower()
        if evidence_lane == "mover+core":
            evidence_lane = "mover"
        lane = lane_payloads.get(evidence_lane)
        if isinstance(lane, dict):
            return _truthy(lane.get("promotion_limited")) or str(
                lane.get("secondary_fallback_status") or ""
            ).lower() in blocked_statuses
        return True
    return str(coverage.get("secondary_fallback_status") or "").lower() in blocked_statuses


def _row_lane_eligible(
    row: dict[str, Any], lane_statuses: dict[str, Any] | None
) -> bool:
    """Require a selected row to use an explicitly eligible evidence lane."""

    if not lane_statuses:
        return True
    universe_lane = str(row.get("universe_lane") or "mover").strip().lower()
    evidence_lane = str(row.get("evidence_lane") or "").strip().lower()
    if universe_lane == "mover+core" and evidence_lane not in {"mover", "core"}:
        return False
    lane = evidence_lane or universe_lane
    if lane not in {"mover", "core"}:
        return False
    payload = lane_statuses.get(lane)
    return isinstance(payload, dict) and payload.get("data_eligible") is True


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


def _plan_qualified(
    row: dict[str, Any],
    *,
    receipt_verifier: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    market_plan = row.get("alphaops_market_structure_plan")
    if not isinstance(market_plan, dict) or not _immutable_plan_provenance(row):
        return False
    plan_hash = str(
        market_plan.get("plan_hash_sha256") or market_plan.get("strategy_plan_hash_sha256") or ""
    ).lower()
    row_plan_hash = str(
        row.get("strategy_plan_hash_sha256") or row.get("plan_hash_sha256") or ""
    ).lower()
    if len(plan_hash) != 64 or plan_hash != row_plan_hash:
        return False
    for row_keys, plan_keys in {
        ("entry_trigger", "entry_watch_level", "entry"): ("entry_trigger", "entry"),
        ("invalidation", "invalidation_level", "stop"): (
            "invalidation",
            "stop",
            "invalidation_level",
        ),
        ("target_1", "target", "first_target"): ("target_1", "target", "first_target"),
    }.items():
        row_value = next(
            (row.get(key) for key in row_keys if row.get(key) is not None), None
        )
        plan_value = next(
            (market_plan.get(key) for key in plan_keys if market_plan.get(key) is not None), None
        )
        if plan_value is not None and _number(row_value) != _number(plan_value):
            return False
    plan_direction = str(market_plan.get("direction") or "").upper()
    row_direction = str(row.get("direction") or row.get("trade_direction") or "").upper()
    if not row_direction or plan_direction != row_direction:
        return False
    from intraday_scanner.alpha.v5_policy import ALPHAOPS_V5_STRATEGY_VERSION

    if (
        not _static_hard_gates(row)
        or not _safe_for_research(row, require_safety=True)
        or str(row.get("strategy_id") or "").lower() != "alphaops_v5"
        or str(row.get("strategy_version") or "") != ALPHAOPS_V5_STRATEGY_VERSION
    ):
        return False
    receipt = str(
        row.get("strategy_receipt_status")
        or row.get("decision_receipt_status")
        or row.get("receipt_status")
        or row.get("strategy_receipt_construction_status")
        or ""
    ).upper()
    if (
        receipt != "COMPLETE"
        or not _valid_hash(str(row.get("receipt_hash_sha256") or "").lower())
        or not str(row.get("receipt_id") or "").strip()
    ):
        return False
    from intraday_scanner.alpha.alert_gate import validate_strategy_receipt_envelope

    if (
        not validate_strategy_receipt_envelope(row)
        or receipt_verifier is None
        or not receipt_verifier(row)
    ):
        return False
    entry = _number(row.get("entry_trigger") or row.get("entry_watch_level") or row.get("entry"))
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
    cost_receipt = row.get("modeled_cost_receipt") or row.get("execution_cost_receipt")
    if not _valid_modeled_cost_receipt(row, plan_hash):
        return False
    qualification_rr = _number(cost_receipt.get("after_cost_reward_risk"))
    return (
        row.get("strategy_receipt_paper_entry_eligible") is True
        and qualification_rr is not None
        and qualification_rr >= 1.5
    )


def _valid_modeled_cost_receipt(row: dict[str, Any], plan_hash: str) -> bool:
    """Validate a content-bound, deterministic cost model receipt.

    A gross ``reward_risk_ratio`` is intentionally never accepted here. The
    receipt must carry after-cost math, the frozen plan identity, and the
    exact policy cost-model version.
    """

    receipt = row.get("modeled_cost_receipt") or row.get("execution_cost_receipt")
    if not isinstance(receipt, dict) or not plan_hash:
        return False
    supplied = str(receipt.get("receipt_hash_sha256") or "").lower()
    payload = {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    if supplied != expected or receipt.get("plan_hash_sha256") != plan_hash:
        return False
    if receipt.get("schema_version") != "dawnstrike.alphaops.modeled_cost_receipt.v1":
        return False
    if receipt.get("cost_model_version") != DEFAULT_V5_POLICY.cost_model_version:
        return False
    if (
        receipt.get("entry_slippage_bps") != DEFAULT_V5_POLICY.entry_slippage_bps
        or receipt.get("exit_slippage_bps") != DEFAULT_V5_POLICY.exit_slippage_bps
        or receipt.get("commission_per_share_per_side")
        != DEFAULT_V5_POLICY.commission_per_share_per_side
        or receipt.get("research_only") is not True
        or receipt.get("broker_execution") != "disabled"
    ):
        return False
    plan = row.get("alphaops_market_structure_plan")
    if not isinstance(plan, dict):
        return False
    try:
        direction = str(plan.get("direction") or "").lower()
        entry = float(plan["entry"])
        stop = float(plan["stop"])
        target = float(plan["target"])
        if direction == "long":
            expected_entry = entry * (1 + DEFAULT_V5_POLICY.entry_slippage_bps / 10_000)
            expected_stop = stop * (1 - DEFAULT_V5_POLICY.exit_slippage_bps / 10_000)
            expected_target = target * (1 - DEFAULT_V5_POLICY.exit_slippage_bps / 10_000)
        elif direction == "short":
            expected_entry = entry * (1 - DEFAULT_V5_POLICY.entry_slippage_bps / 10_000)
            expected_stop = stop * (1 + DEFAULT_V5_POLICY.exit_slippage_bps / 10_000)
            expected_target = target * (1 + DEFAULT_V5_POLICY.exit_slippage_bps / 10_000)
        else:
            return False
        commission = DEFAULT_V5_POLICY.commission_per_share_per_side * 2
        if direction == "long":
            expected_risk = expected_entry - expected_stop + commission
            expected_reward = expected_target - expected_entry - commission
        else:
            expected_risk = expected_stop - expected_entry + commission
            expected_reward = expected_entry - expected_target - commission
        expected_ratio = expected_reward / expected_risk
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False

    def matches(name: str, expected_value: float) -> bool:
        actual = _number(receipt.get(name))
        return actual is not None and abs(actual - expected_value) <= 1e-7

    return (
        receipt.get("direction") == direction
        and
        matches("entry_price", entry)
        and matches("stop_price", stop)
        and matches("target_price", target)
        and matches("expected_entry_price", expected_entry)
        and matches("expected_stop_exit_price", expected_stop)
        and matches("expected_target_exit_price", expected_target)
        and matches("risk_per_share_after_cost", expected_risk)
        and matches("reward_per_share_after_cost", expected_reward)
        and matches("after_cost_reward_risk", expected_ratio)
        and expected_ratio > 0
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
    ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
    plan = row.get("alphaops_market_structure_plan")
    plan_hash = str(plan.get("plan_hash_sha256") or "") if isinstance(plan, dict) else ""
    if (
        not signal_id
        or not ticker
        or not plan_hash
        or str(proof.get("signal_id") or "") != signal_id
        or str(proof.get("ticker") or proof.get("symbol") or "").upper() != ticker
    ):
        return False
    if str(proof.get("plan_hash_sha256") or "") != plan_hash:
        return False
    lineage = _frozen_lineage_for_validation(row, ticker)
    required_lineage = (
        "selection_id",
        "cohort",
        "source_scan_id",
        "frozen_slate_id",
        "frozen_slate_content_hash_sha256",
        "frozen_research_selection_id",
    )
    if set(required_lineage) - set(lineage) or any(
        not lineage.get(field) for field in required_lineage
    ):
        return False
    if lineage.get("cohort") != "official_telegram":
        return False
    for field in required_lineage:
        if str(proof.get(field) or "") != str(lineage[field]):
            return False
    checked_at = _parse_watcher_time(proof.get("checked_at"))
    if checked_at is None or abs((datetime.now(timezone.utc) - checked_at).total_seconds()) > 900:
        return False
    row_market_date = str(row.get("market_date") or row.get("generated_at") or "")[:10]
    if row_market_date and checked_at.astimezone(EASTERN).date().isoformat() != row_market_date:
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
        if (
            str(receipt.get("signal_id") or "") != signal_id
            or str(receipt.get("plan_hash_sha256") or "") != plan_hash
            or str(receipt.get("ticker") or receipt.get("symbol") or "").upper() != ticker
            or receipt.get("research_only") is not True
            or receipt.get("broker_execution") != "disabled"
            or (lineage and any(
                expected
                and str(receipt.get(field) or "") != str(expected)
                for field, expected in lineage.items()
            ))
        ):
            return False
    quote = proof["quote_receipt"]
    quote_raw_json = str(quote.get("quote_raw_payload_json") or "")
    quote_source_hash = str(quote.get("source_quote_hash_sha256") or "").lower()
    try:
        quote_raw = json.loads(quote_raw_json)
        quote_raw_canonical = json.dumps(
            quote_raw, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    raw_quote = quote_raw.get("quote") if isinstance(quote_raw, dict) else None
    quote_time = _parse_watcher_time(quote.get("observed_at"))
    bid = _number(quote.get("bid"))
    ask = _number(quote.get("ask"))
    last = _number(quote.get("last") or quote.get("price"))
    current_price = _number(
        row.get("current_price")
        or row.get("current_quote_price")
        or row.get("observed_price")
    )
    plan_entry = _number(plan.get("entry") if isinstance(plan, dict) else None)
    plan_stop = _number(plan.get("stop") if isinstance(plan, dict) else None)
    plan_target = _number(plan.get("target") if isinstance(plan, dict) else None)
    plan_direction = str(plan.get("direction") or "").lower() if isinstance(plan, dict) else ""
    spread_pct = ((ask - bid) / ((ask + bid) / 2.0) * 100.0) if bid and ask else None
    trigger_consistent = (
        plan_entry is not None
        and plan_stop is not None
        and plan_target is not None
        and last is not None
        and (
            (
                plan_direction == "long"
                and plan_stop < plan_entry <= last < plan_target
                and ((last - plan_entry) / plan_entry * 100.0) <= 2.0
            )
            or (
                plan_direction == "short"
                and plan_target < last <= plan_entry < plan_stop
                and ((plan_entry - last) / plan_entry * 100.0) <= 2.0
            )
        )
    )
    if (
        str(quote.get("schema_version") or "") != "dawnstrike.alphaops.quote_receipt.v1"
        or str(quote.get("status") or "").upper() != "USABLE"
        or not str(quote.get("source") or "").lower().startswith("alpaca_market_data_")
        or quote_time is None
        or quote_time > checked_at
        or (checked_at - quote_time).total_seconds() > 360
        or bid is None
        or ask is None
        or bid <= 0
        or ask < bid
        or last is None
        or current_price is None
        or abs(last - current_price) > 1e-9
        or _number(quote.get("entry_reference")) != plan_entry
        or str(quote.get("entry_window_status") or "").upper() != "OPEN"
        or str(quote.get("trigger_status") or "").upper() != "CONFIRMED"
        or spread_pct is None
        or spread_pct > 2.0
        or not trigger_consistent
        or quote.get("quote_side")
        != ("ask" if plan_direction == "long" else "bid")
        or _number(quote.get("decision_price")) != last
        or last != (ask if plan_direction == "long" else bid)
        or not _valid_hash(quote_source_hash)
        or hashlib.sha256(quote_raw_canonical.encode()).hexdigest() != quote_source_hash
        or not isinstance(raw_quote, dict)
        or str(quote_raw.get("ticker") or "").upper() != ticker
        or _number(raw_quote.get("bp")) != bid
        or _number(raw_quote.get("ap")) != ask
        or _parse_watcher_time(raw_quote.get("t"))
        != _parse_watcher_time(quote.get("observed_at"))
    ):
        return False
    portfolio = proof["portfolio_receipt"]
    portfolio_time = _parse_watcher_time(portfolio.get("checked_at"))
    portfolio_account_id = str(portfolio.get("simulated_account_id") or "").strip()
    row_account_id = str(row.get("account_id") or "").strip()
    decision_trace = row.get("decision_trace")
    trace_account_id = (
        str(decision_trace.get("account_id") or "").strip()
        if isinstance(decision_trace, dict)
        else ""
    )
    trace = proof.get("evaluate_v5_official_paper")
    if (
        str(portfolio.get("schema_version") or "")
        != "dawnstrike.alphaops.portfolio_admission.v1"
        or str(portfolio.get("status") or "").upper() != "ADMITTED"
        or portfolio.get("admitted") is not True
        or list(portfolio.get("blocking_reasons") or [])
        or str(portfolio.get("account_mode") or "").upper() != "PAPER"
        or portfolio_account_id != ALPHAOPS_V5_ACCOUNT_ID
        or (row_account_id and row_account_id != portfolio_account_id)
        or (trace_account_id and trace_account_id != portfolio_account_id)
        or not str(portfolio.get("admission_id") or "").strip()
        or str(portfolio.get("admission_key") or "")
        != f"paper-admission:{signal_id}:{plan_hash[:16]}"
        or portfolio_time is None
        or abs((checked_at - portfolio_time).total_seconds()) > 60
        or not isinstance(trace, dict)
        or trace.get("account_id") != ALPHAOPS_V5_ACCOUNT_ID
    ):
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


def _frozen_lineage_for_validation(
    row: dict[str, Any], ticker: str
) -> dict[str, str]:
    """Extract the authoritative frozen slate identity carried by a signal."""

    slate = row.get("frozen_ranked_research_slate")
    lineage = row.get("frozen_slate_lineage")
    if not isinstance(slate, dict):
        payload = row.get("payload_json") or row.get("selection_payload_json")
        if isinstance(payload, dict):
            slate = payload.get("frozen_ranked_research_slate")
            lineage = payload.get("frozen_slate_lineage")
    if not isinstance(slate, dict):
        return {}
    try:
        validate_ranked_research_slate(
            slate, market_date=str(row.get("market_date") or "")[:10]
        )
    except (TypeError, ValueError):
        return {"_invalid": "frozen ranked slate failed validation"}
    rows = slate.get("rows") or []
    member = next(
        (
            str(item.get("research_selection_id") or "")
            for item in rows
            if isinstance(item, dict)
            and str(item.get("ticker") or item.get("symbol") or "").upper() == ticker
        ),
        "",
    )
    values = {
        "selection_id": str(row.get("selection_id") or ""),
        "cohort": str(row.get("cohort") or ""),
        "source_scan_id": str(
            (lineage or {}).get("frozen_source_scan_id")
            if isinstance(lineage, dict)
            else row.get("source_scan_id") or row.get("scan_id") or ""
        ),
        "frozen_slate_id": str(slate.get("slate_id") or ""),
        "frozen_slate_content_hash_sha256": str(
            slate.get("content_hash_sha256") or ""
        ),
        "frozen_research_selection_id": member,
    }
    if values["source_scan_id"] != str(slate.get("scan_id") or ""):
        return {"_invalid": "frozen source scan does not match slate scan"}
    selection_ids = {str(item) for item in slate.get("selection_ids") or []}
    if member and selection_ids and member not in selection_ids:
        return {"_invalid": "selection member is not in frozen slate"}
    return {key: value for key, value in values.items() if value}


def validate_watcher_current_proof(row: dict[str, Any]) -> bool:
    """Public strict validator shared by watcher production and publication."""

    return _watcher_current(dict(row))


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
    contract = row.get("alphaops_market_structure_plan")
    if not isinstance(contract, dict) or str(contract.get("status") or "").upper() != "COMPLETE":
        return False
    try:
        from intraday_scanner.alpha.plan_constructor import validate_alphaops_v5_plan
    except ImportError:
        return False
    try:
        if validate_alphaops_v5_plan(contract) is False:
            return False
    except (TypeError, ValueError):
        return False
    return _valid_hash(str(contract.get("plan_hash_sha256") or "").lower())


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
    source_signal_id = str(
        output.get("signal_id")
        or output.get("signal_key")
        or f"luna-research:{output['ticker']}:{rank}"
    )
    output["research_source_signal_id"] = source_signal_id
    selection_basis = f"{output['ticker']}|{source_signal_id}"
    output["research_selection_id"] = "luna-research:" + hashlib.sha256(
        selection_basis.encode("utf-8")
    ).hexdigest()[:24]
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
    "WAITING_EXECUTION_COSTS",
    "apply_publication_semantics",
    "build_ranked_research_slate",
    "official_publication_rows",
    "persist_ranked_research_slate",
    "publication_counts",
    "validated_frozen_selection_signal",
    "validate_watcher_current_proof",
    "validate_ranked_research_slate",
]
