"""Governed adapter from immutable prior-session PaperOps to Morning research.

PaperOps v2 is a historical, management-only series.  This module never turns
its rows into orders.  It reads only the exact prior forward-session decision
cohort, proves the ledger/pick/run/DataTruth/registry bindings, reuses the
strategy's deterministic core predicate as lineage, and binds all current
market and safety evidence from Morning's point-in-time rows before the normal
strategy-receipt service evaluates the candidate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.market_calendar import market_session
from intraday_scanner.services.luna_research_slate_service import row_research_admissible
from intraday_scanner.v2.data_truth.core import (
    DATA_TRUTH_MANIFEST_SCHEMA_VERSION,
    SNAPSHOT_ARTIFACT_SCHEMA_VERSION,
    _manifest_from_payload,
    _manifest_payload_hash,
    _snapshot_content_hash_from_hashes,
    _snapshot_id,
)
from intraday_scanner.v2.paper_ops.models import (
    LEGACY_PAPER_EXECUTION_POLICY_VERSION,
    PAPER_EXECUTION_POLICY_VERSION,
)
from intraday_scanner.v2.paper_ops.storage import read_json, read_jsonl

ADAPTER_SCHEMA_VERSION = "dawnstrike.morning.prior_paper_ops_adapter.v2"
LEGACY_SOURCE_LABEL = "prior_session_paper_ops_legacy_source"
GOVERNED_SOURCE_LABEL = "prior_session_paper_ops_governed_source"
MIN_REWARD_RISK = 1.50
MAX_STOP_DISTANCE_PCT = 0.15
DEFAULT_CURRENT_FRESHNESS_MAX_AGE_SECONDS = 1_200
DATATRUTH_MANIFEST_ATTESTATION_LABEL = (
    "VERIFIED_DATATRUTH_MANIFEST_ATTESTATION_ACL_SEALED"
)
_ACCEPTED_SOURCE_POLICIES = frozenset(
    {LEGACY_PAPER_EXECUTION_POLICY_VERSION, PAPER_EXECUTION_POLICY_VERSION}
)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CODE_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def adapt_prior_session_paper_ops(
    *,
    output_root: str | Path | None,
    market_date: str,
    current_candidates: Sequence[Mapping[str, Any]] | None,
    current_snapshot_id: str,
    current_source_identity: str,
    current_code_sha: str,
    current_universe_membership: Sequence[str] | None = None,
    current_core_membership: Sequence[str] | None = None,
    decision_at: str,
    store: Any | None = None,
) -> dict[str, Any]:
    """Return raw, receipt-ready candidates from one verified prior session.

    ``store`` is accepted for source compatibility but deliberately unused:
    the canonical Morning receipt pipeline is the only receipt writer.
    Missing/invalid PaperOps evidence produces a structured blocked result and
    an empty row set; it never manufactures a signal.
    """

    del store
    requested_date = _valid_date(market_date)
    base = _result_base(requested_date)
    if requested_date is None:
        base.update({"status": "BLOCKED_INVALID_MARKET_DATE", "reason": "invalid market date"})
        return base
    if not str(current_snapshot_id or "").strip() or not str(current_source_identity or "").strip():
        base.update(
            {
                "status": "BLOCKED_MISSING_CURRENT_BINDING",
                "reason": "current snapshot and source identity are required",
            }
        )
        return base
    if not str(current_code_sha or "").strip() or not _CODE_SHA.fullmatch(
        str(current_code_sha).lower()
    ):
        base.update(
            {
                "status": "BLOCKED_MISSING_CODE_IDENTITY",
                "reason": "current code SHA is required for a decision receipt",
            }
        )
        return base
    if output_root in {None, ""}:
        base.update({"status": "NOT_CONFIGURED", "reason": "paper_ops_root was not provided"})
        return base
    root = Path(output_root)
    try:
        prior_date = _prior_session(requested_date)
    except (TypeError, ValueError):
        base.update(
            {"status": "BLOCKED_INVALID_MARKET_CALENDAR", "reason": "prior session unavailable"}
        )
        return base
    try:
        from intraday_scanner.v2.paper_ops.trade_blotter import load_trade_blotter_readonly

        materialized = load_trade_blotter_readonly(
            output_root=root,
            mode="forward",
            run_date=prior_date.isoformat(),
            series_role="champion",
        )
        prior_rows = [dict(row) for row in materialized]
        validated = _validate_prior_session_bindings(
            root,
            prior_date=prior_date.isoformat(),
            rows=prior_rows,
        )
    except Exception as exc:  # observer/data lineage failures remain fail-closed
        base.update(
            {
                "status": "BLOCKED_PRIOR_SESSION_EVIDENCE",
                "reason": f"{type(exc).__name__}: {exc}",
                "prior_session_date": prior_date.isoformat(),
            }
        )
        return base
    exact_rows = validated["accepted_rows"]
    current_rows = [dict(row) for row in (current_candidates or [])]
    union_membership = {
        str(item).strip().upper()
        for item in (*list(current_universe_membership or ()), *list(current_core_membership or ()))
        if str(item).strip()
    }
    union_membership.update(
        str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        for row in current_rows
        if str(row.get("ticker") or row.get("symbol") or "").strip()
    )
    adapter_provenance = {
        **dict(validated["provenance"]),
        "current_code_sha": str(current_code_sha).lower(),
        "paper_ops_root": str(root.resolve()),
        "current_snapshot_id": str(current_snapshot_id),
        "current_source_identity": str(current_source_identity),
        "current_market_date": requested_date.isoformat(),
        "decision_at": str(decision_at),
    }
    adapted = adapt_verified_prior_session_rows(
        exact_rows,
        current_rows=current_rows,
        prior_session_date=prior_date.isoformat(),
        current_market_date=requested_date.isoformat(),
        current_snapshot_id=str(current_snapshot_id),
        current_source_identity=str(current_source_identity),
        decision_at=str(decision_at),
        current_universe_membership=union_membership,
        provenance=adapter_provenance,
    )
    base.update(
        {
            "status": "READY" if adapted else "NO_QUALIFIED_CANDIDATES",
            "reason": "" if adapted else "no prior accepted strategy row passed current gates",
            "prior_session_date": prior_date.isoformat(),
            "prior_row_count": len(exact_rows),
            "adapted_count": len(adapted),
            "rows": adapted,
            "provenance": adapter_provenance,
            "current_code_sha": str(current_code_sha).lower(),
            "paper_ops_root": str(root.resolve()),
            "source_policy_versions": list(validated["source_policy_versions"]),
            "datatruth_binding_statuses": list(
                validated["provenance"].get("datatruth_binding_statuses") or []
            ),
            "enabled_strategy_ids": list(validated["enabled_strategy_ids"]),
            "enabled_strategy_identities": dict(
                validated["enabled_strategy_identities"]
            ),
            "strategy_contributions": _contribution_summary(
                adapted, validated["enabled_strategy_ids"]
            ),
            "research_only": True,
            "broker_execution_enabled": False,
        }
    )
    return base


def adapt_verified_prior_session_rows(
    prior_rows: Sequence[Mapping[str, Any]],
    *,
    current_rows: Sequence[Mapping[str, Any]],
    prior_session_date: str,
    current_market_date: str,
    current_snapshot_id: str,
    current_source_identity: str,
    decision_at: str,
    current_universe_membership: set[str] | Sequence[str],
    provenance: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Adapt already-proven rows without performing external I/O.

    One row is emitted per contributing strategy.  Alpha's canonical receipt
    pass runs on that complete set, after which the cycle groups same-ticker
    contributors into one frozen slate row.
    """

    if not _valid_date(prior_session_date) or not _valid_date(current_market_date):
        return []
    if not str(current_snapshot_id or "").strip() or not str(current_source_identity or "").strip():
        return []
    decision_dt = _parse_aware_datetime(decision_at)
    current_date = _valid_date(current_market_date)
    if decision_dt is None or current_date is None or decision_dt.date() != current_date:
        return []
    memberships = {
        str(item).strip().upper()
        for item in current_universe_membership
        if str(item).strip()
    }
    current_by_symbol: dict[str, dict[str, Any]] = {}
    for source in current_rows:
        row = dict(source)
        ticker = _ticker(row)
        if not ticker or ticker in current_by_symbol:
            continue
        if ticker not in memberships or not _current_lane_ok(row):
            continue
        observed_at = _validated_current_observed_at(
            row,
            decision_at=decision_dt,
            current_market_date=current_date,
        )
        if observed_at is None:
            continue
        # Alpha's enrichment contract records freshness as
        # ``enrichment_status``/``source_quality_status`` rather than the
        # generic publication key.  Normalize that existing value for the
        # shared safety predicate; do not default missing truth to a pass.
        admission_row = dict(row)
        if not admission_row.get("freshness_status"):
            admission_row["freshness_status"] = (
                admission_row.get("enrichment_status")
                or admission_row.get("source_quality_status")
                or ""
            )
        if _current_fallback_blocked(admission_row):
            continue
        if not row_research_admissible(admission_row):
            continue
        admission_row["_adapter_current_observed_at"] = observed_at
        current_by_symbol[ticker] = admission_row

    identities = _catalog_identities()
    output: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()
    for source in sorted(
        (dict(row) for row in prior_rows),
        key=lambda row: (
            _ticker(row),
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("signal_id") or ""),
        ),
    ):
        ticker = _ticker(source)
        strategy_id = str(source.get("strategy_id") or "").strip()
        strategy_version = str(source.get("strategy_version") or "").strip()
        strategy_fp = str(source.get("strategy_semantics_fingerprint") or "").strip().lower()
        source_policy = str(source.get("execution_policy_version") or "").strip()
        signal_id = str(source.get("signal_id") or "").strip()
        if (
            not ticker
            or ticker not in current_by_symbol
            or not signal_id
            or signal_id in seen_source_ids
            or strategy_id not in identities
            or identities[strategy_id][0] != strategy_version
            or identities[strategy_id][1] != strategy_fp
            or source_policy not in _ACCEPTED_SOURCE_POLICIES
            or not _levels_pass(source)
        ):
            continue
        seen_source_ids.add(signal_id)
        current = current_by_symbol[ticker]
        if not _current_setup_open(
            current,
            direction=str(source.get("direction") or "").lower(),
            stop=_number(source.get("stop")),
            target=_number(source.get("target")),
        ):
            continue
        output.append(
            _adapt_one(
                source,
                current=current,
                current_market_date=current_market_date,
                prior_session_date=prior_session_date,
                current_snapshot_id=current_snapshot_id,
                current_source_identity=current_source_identity,
                decision_at=decision_at,
                provenance=provenance or {},
            )
        )
    return output


def _adapt_one(
    source: dict[str, Any],
    *,
    current: dict[str, Any],
    current_market_date: str,
    prior_session_date: str,
    current_snapshot_id: str,
    current_source_identity: str,
    decision_at: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    ticker = _ticker(source)
    strategy_id = str(source["strategy_id"])
    strategy_version = str(source["strategy_version"])
    source_signal_id = str(source["signal_id"])
    prior_entry = _number(source.get("entry_reference"))
    prior_stop = _number(source.get("stop"))
    prior_target = _number(source.get("target"))
    direction = str(source.get("direction") or "").lower()
    source_policy = str(source.get("execution_policy_version") or "").strip()
    source_label = (
        LEGACY_SOURCE_LABEL
        if source_policy == LEGACY_PAPER_EXECUTION_POLICY_VERSION
        else GOVERNED_SOURCE_LABEL
    )
    current_observed_at = _current_observed_at(current)
    prior_observed_at = _prior_observed_at(source, prior_session_date)
    common = _current_condition_results(
        current,
        ticker=ticker,
        current_observed_at=current_observed_at,
        current_source_identity=current_source_identity,
        entry=prior_entry,
        stop=prior_stop,
        target=prior_target,
        direction=direction,
    )
    core = {
        spec.condition_id: {
            "condition_id": spec.condition_id,
            "status": "PASS",
            "observed_value": {
                "source": "immutable_paper_pick_decision",
                "record_id": source_signal_id,
                "prior_session_date": prior_session_date,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
            },
            "reason": "accepted prior strategy signal and exact source binding verified",
            "observed_at": prior_observed_at,
            "effective_at": prior_observed_at,
        }
        for spec in _strategy_core_specs(strategy_id)
    }
    row = {
        **current,
        "ticker": ticker,
        "symbol": ticker,
        "market_date": current_market_date,
        "timestamp": decision_at,
        "decision_at": decision_at,
        "source_identity": current_source_identity,
        "scan_id": current_snapshot_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "strategy_semantics_fingerprint": str(source["strategy_semantics_fingerprint"]),
        "strategy_status": str(source.get("strategy_status") or "experimental"),
        "direction": direction,
        "entry_reference": prior_entry,
        "entry_watch_level": prior_entry,
        "stop": prior_stop,
        "invalidation_level": prior_stop,
        "target": prior_target,
        "target_1": prior_target,
        "reward_risk_ratio": _number(source.get("reward_risk")),
        "base_strategy_score": _number(source.get("setup_score")) or 0.0,
        "alpha_score": _number(source.get("setup_score")) or 0.0,
        "signal_id": _source_signal_id(
            current_market_date,
            ticker,
            strategy_id,
            strategy_version,
            source_signal_id,
        ),
        "source_signal_id": source_signal_id,
        "prior_session_signal_id": source_signal_id,
        "prior_session_paper_ops": {
            "source_label": source_label,
            "source_signal_id": source_signal_id,
            "source_run_id": str(source.get("run_id") or ""),
            "source_pick_id": str(source.get("pick_id") or source_signal_id),
            "source_data_snapshot_id": str(source.get("data_snapshot_id") or ""),
            "source_execution_policy_version": str(source["execution_policy_version"]),
            "source_lifecycle_status": str(source.get("lifecycle_status") or ""),
            "source_decision_status": str(source.get("decision_status") or "accepted"),
            "source_semantics_fingerprint": str(source.get("strategy_semantics_fingerprint") or ""),
            "source_policy": source_policy,
            "source_provenance": dict(provenance),
        },
        "strategy_adapter": source_label,
        "strategy_core_lineage": core,
        "condition_results": {**common, **core},
        "research_only": True,
        "broker_execution": "disabled",
        "broker_execution_enabled": False,
        "can_alert": False,
        "alert_sent": False,
        "fixture_only": False,
        "research_adapter_re_gated": True,
        "research_adapter_min_reward_risk": MIN_REWARD_RISK,
        "research_adapter_max_stop_distance_pct": MAX_STOP_DISTANCE_PCT,
        "research_adapter_current_snapshot_id": current_snapshot_id,
        "research_adapter_current_source_identity": current_source_identity,
        "research_adapter_source_policy": source_policy,
        "research_adapter_current_observed_at": current_observed_at,
    }
    return row


def _current_condition_results(
    row: Mapping[str, Any],
    *,
    ticker: str,
    current_observed_at: str,
    current_source_identity: str,
    entry: float | None,
    stop: float | None,
    target: float | None,
    direction: str,
) -> dict[str, dict[str, Any]]:
    """Build current evidence only; prior strategy conditions are separate."""

    def result(
        condition_id: str,
        status: str,
        observed: Any = None,
        reason: str = "",
    ) -> dict[str, Any]:
        return {
            "condition_id": condition_id,
            "status": status,
            "observed_value": observed,
            "reason": reason or f"current row {status.lower()}",
            "observed_at": current_observed_at,
            "effective_at": current_observed_at,
        }

    freshness = str(
        row.get("freshness_status")
        or row.get("freshness_verdict")
        or row.get("data_freshness")
        or row.get("enrichment_status")
        or row.get("source_quality_status")
        or ""
    ).upper()
    halt = str(row.get("halt_status") or "").upper()
    source_identity = str(
        row.get("source_identity")
        or row.get("source")
        or row.get("enrichment_primary_source")
        or row.get("data_snapshot_id")
        or current_source_identity
        or ""
    ).strip()
    current_price = _number(
        row.get("current_price")
        or row.get("premarket_price")
        or row.get("price")
        or row.get("close")
    )
    current_volume = _number(
        row.get("current_volume")
        or row.get("volume")
        or row.get("avg_volume")
        or row.get("premarket_volume")
    )
    timestamp = _current_observed_at(row)
    conflict = (
        row.get("conflict_flags")
        or row.get("provider_conflict")
        or row.get("evidence_conflict")
    )
    conflict_status = str(
        row.get("evidence_status") or row.get("safety_evidence_status") or ""
    ).upper()
    spread = _number(row.get("spread_pct"))
    risk_budget = _number(
        row.get("risk_budget")
        or row.get("max_risk_budget")
        or row.get("risk_capacity")
    )
    risk_per_unit = abs(entry - stop) if entry is not None and stop is not None else None
    rr = (
        abs(target - entry) / risk_per_unit
        if target is not None and entry is not None and risk_per_unit not in {None, 0}
        else None
    )
    stop_distance_pct = (
        abs(entry - stop)
        / entry
        if entry is not None and entry > 0 and stop is not None
        else math.inf
    )
    geometry = (
        entry is not None
        and stop is not None
        and entry > 0
        and ((direction == "long" and stop < entry) or (direction == "short" and stop > entry))
    )
    values: dict[str, dict[str, Any]] = {
        "valid_symbol": result(
            "valid_symbol",
            "PASS" if _SYMBOL.fullmatch(ticker) else "FAIL",
            ticker,
        ),
        "point_in_time_ohlcv": result(
            "point_in_time_ohlcv",
            "PASS"
            if timestamp and (current_price is not None or current_volume is not None)
            else "MISSING_DISCLOSED",
            timestamp or None,
            (
                "current observation timestamp is missing"
                if not timestamp
                else "current observation is bound"
            ),
        ),
        "positive_current_price": result(
            "positive_current_price",
            "PASS"
            if current_price is not None and current_price > 0
            else "MISSING_DISCLOSED",
            current_price,
            (
                "current price is missing or non-positive"
                if current_price is None or current_price <= 0
                else ""
            ),
        ),
        "positive_current_volume": result(
            "positive_current_volume",
            "PASS"
            if current_volume is not None and current_volume > 0
            else "MISSING_DISCLOSED",
            current_volume,
            (
                "current volume is missing or non-positive"
                if current_volume is None or current_volume <= 0
                else ""
            ),
        ),
        "source_identity_present": result(
            "source_identity_present",
            "PASS" if source_identity else "MISSING_DISCLOSED",
            source_identity,
        ),
        "source_fresh": result(
            "source_fresh",
            "PASS"
            if freshness in {"FRESH", "VERIFIED", "CURRENT", "PASS", "LIMITED"}
            else "MISSING_DISCLOSED",
            freshness or None,
        ),
        "no_market_source_conflict": result(
            "no_market_source_conflict",
            "FAIL"
            if conflict or conflict_status in {"CONFLICT", "INCOMPLETE"}
            else (
                "PASS"
                if (
                    "conflict_flags" in row
                    or "provider_conflict" in row
                    or "evidence_conflict" in row
                    or str(row.get("source_quality_status") or "").upper()
                    in {"VERIFIED", "PASS", "CLEAR", "LIMITED"}
                )
                else "MISSING_DISCLOSED"
            ),
            conflict if conflict is not None else conflict_status or None,
        ),
        "not_currently_halted": result(
            "not_currently_halted",
            "PASS"
            if halt in {"CLEAR", "PASS", "VERIFIED", "NONE", "NO_RISK"}
            else "MISSING_DISCLOSED",
            halt or None,
        ),
        "valid_entry_reference": result(
            "valid_entry_reference",
            "PASS" if entry is not None and entry > 0 else "FAIL",
            entry,
        ),
        "valid_stop_geometry": result(
            "valid_stop_geometry",
            "PASS" if geometry else "FAIL",
            {"direction": direction, "entry": entry, "stop": stop},
        ),
        "valid_target_when_required": result(
            "valid_target_when_required", "PASS" if target is not None else "FAIL", target
        ),
        "reward_risk_at_least_1_50": result(
            "reward_risk_at_least_1_50",
            "PASS" if rr is not None and rr >= MIN_REWARD_RISK - 1e-12 else "FAIL",
            rr,
        ),
        "within_risk_budget": result(
            "within_risk_budget",
            "PASS"
            if (
                risk_per_unit is not None
                and stop_distance_pct <= MAX_STOP_DISTANCE_PCT + 1e-12
                and (risk_budget is None or risk_per_unit <= risk_budget)
            )
            else "MISSING_DISCLOSED",
            {
                "risk_per_unit": risk_per_unit,
                "risk_budget": risk_budget,
                "stop_distance_pct": stop_distance_pct,
                "max_stop_distance_pct": MAX_STOP_DISTANCE_PCT,
                "budget_basis": (
                    "verified_stop_distance_cap"
                    if risk_budget is None
                    else "current_risk_budget_and_verified_stop_distance_cap"
                ),
            },
        ),
        "spread_within_existing_policy": result(
            "spread_within_existing_policy",
            "PASS" if spread is not None and 0 <= spread <= 3.0 else "MISSING_DISCLOSED",
            spread,
        ),
    }
    return values


def _validate_prior_session_bindings(
    root: Path,
    *,
    prior_date: str,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prove one exact PaperOps scan cohort and all immutable origin inputs."""

    from intraday_scanner.v2.paper_ops import engine

    paths = engine.PaperOpsPaths.resolve(root)
    events = [
        event
        for event in read_jsonl(paths.ledger / "paper_ledger.jsonl")
        if event.get("event_type") in {"paper_pick_decision", "paper_no_setup_decision"}
        and event.get("mode") == "forward"
        and event.get("trade_date") == prior_date
    ]
    if not events:
        raise ValueError("prior session has no paper_pick_decision ledger rows")
    pick_payloads = read_json(paths.exports / f"picks_forward_{prior_date}.json", None)
    decision_payloads = read_json(
        paths.exports / f"strategy_decisions_forward_{prior_date}.json", None
    )
    if not isinstance(pick_payloads, list) or not isinstance(decision_payloads, list):
        raise ValueError("prior session pick/decision projections are missing")
    engine._validate_run_and_origin_evidence(paths, events, {})
    engine._validate_scan_artifact_evidence(events, pick_payloads, decision_payloads)
    pick_by_id = {
        str(row.get("pick_id") or ""): row
        for row in pick_payloads
        if isinstance(row, dict) and str(row.get("pick_id") or "")
    }
    event_by_id = {
        str(event.get("payload", {}).get("pick_id") or ""): event
        for event in events
        if isinstance(event.get("payload"), dict)
    }
    accepted_rows: list[dict[str, Any]] = []
    run_ids: set[str] = set()
    snapshots: set[str] = set()
    manifest_hashes: set[str] = set()
    source_policies: set[str] = set()
    datatruth_binding_statuses: set[str] = set()
    for row in rows:
        if str(row.get("decision_status") or "") != "accepted":
            continue
        if str(row.get("signal_date") or "") != prior_date:
            continue
        if str(row.get("series_role") or "") != "champion":
            continue
        pick_id = str(row.get("signal_id") or "")
        event = event_by_id.get(pick_id)
        if event is None or not isinstance(event.get("payload"), dict):
            raise ValueError(f"accepted blotter row lacks exact ledger pick {pick_id}")
        payload = dict(event["payload"])
        if pick_by_id.get(str(payload.get("pick_id") or "")) != payload:
            raise ValueError(f"accepted pick projection conflicts for {pick_id}")
        source_policy = str(payload.get("execution_policy_version") or "")
        if source_policy not in _ACCEPTED_SOURCE_POLICIES:
            raise ValueError("prior source execution policy is not governed")
        for field in (
            "strategy_id",
            "strategy_version",
            "strategy_semantics_fingerprint",
            "execution_policy_version",
            "symbol",
            "direction",
            "entry_reference",
            "stop",
            "target",
        ):
            if field == "symbol":
                expected = str(payload.get(field) or "").upper()
                actual = str(row.get(field) or "").upper()
            else:
                expected = payload.get(field)
                actual = row.get(field)
            if expected != actual:
                raise ValueError(f"blotter lineage conflicts for {pick_id}: {field}")
        run_id = str(payload.get("run_id") or "")
        manifest_path = paths.manifests / f"{engine._safe_filename(run_id)}.json"
        manifest = read_json(manifest_path, None)
        if not isinstance(manifest, dict):
            raise ValueError(f"run manifest missing for {run_id}")
        _validate_manifest_hash(manifest)
        if manifest.get("run_id") != run_id or manifest.get("run_date") != prior_date:
            raise ValueError("prior run manifest identity conflicts")
        if str(manifest.get("execution_policy_version") or "") != source_policy:
            raise ValueError("prior run manifest execution policy conflicts")
        if manifest.get("data_snapshot_id") != payload.get("run_id", "").split(":")[-1]:
            raise ValueError("prior run snapshot identity conflicts")
        if (
            str(row.get("run_id") or "") != run_id
            or str(row.get("data_snapshot_id") or "")
            != str(manifest.get("data_snapshot_id") or "")
        ):
            raise ValueError("prior blotter snapshot lineage conflicts")
        try:
            engine._load_bound_run_dataset(paths, manifest, required=True)
            datatruth_binding_statuses.add("VERIFIED_DATATRUTH_BYTES")
        except PermissionError:
            _attest_bound_datatruth_manifest(paths, manifest)
            datatruth_binding_statuses.add(DATATRUTH_MANIFEST_ATTESTATION_LABEL)
        run_ids.add(run_id)
        snapshots.add(str(manifest.get("data_snapshot_id") or ""))
        manifest_hashes.add(str(manifest.get("manifest_payload_hash") or ""))
        accepted_row = dict(row)
        # The read-only lifecycle projection intentionally omits the original
        # pick's reward/risk field.  Carry it forward only after the exact
        # ledger/pick equality checks above, so level gating never trusts the
        # mutable aggregate for identity.
        accepted_row["reward_risk"] = payload.get("reward_risk")
        accepted_row["pick_id"] = str(payload.get("pick_id") or pick_id)
        accepted_row["evidence"] = list(payload.get("evidence") or [])
        accepted_row["source_execution_policy_version"] = source_policy
        accepted_rows.append(accepted_row)
        source_policies.add(source_policy)
    if not accepted_rows:
        raise ValueError("prior session has no accepted champion strategy rows")
    identities = _catalog_identities()
    registry_rows = read_json(paths.state / "strategy_registry.json", None)
    if not isinstance(registry_rows, list):
        raise ValueError("strategy registry is missing")
    registry_keys = {
        (
            str(item.get("strategy_id") or ""),
            str(item.get("strategy_version") or ""),
            str(item.get("execution_policy_version") or ""),
            str(item.get("strategy_semantics_fingerprint") or ""),
        )
        for item in registry_rows
        if isinstance(item, dict)
    }
    enabled_rows = [
        row
        for row in registry_rows
        if str(row.get("strategy_id") or "") in identities
        and str(row.get("strategy_version") or "") == identities[
            str(row.get("strategy_id") or "")
        ][0]
        and str(row.get("strategy_semantics_fingerprint") or "")
        == identities[str(row.get("strategy_id") or "")][1]
        and str(row.get("execution_policy_version") or "") in source_policies
        and row.get("allow_entries") is True
        and str(row.get("paper_status") or "") == "eligible"
    ]
    enabled_keys = {
        (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or ""),
            str(row.get("strategy_semantics_fingerprint") or ""),
        )
        for row in enabled_rows
    }
    for row in accepted_rows:
        key = (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or ""),
            str(row.get("strategy_semantics_fingerprint") or ""),
        )
        if key not in registry_keys or key not in enabled_keys or key[0] not in identities:
            raise ValueError("accepted strategy identity is not exact in active registry")
        if key[2] not in _ACCEPTED_SOURCE_POLICIES:
            raise ValueError("accepted strategy policy is not governed")
    pick_bytes_hash = hashlib.sha256(
        (paths.exports / f"picks_forward_{prior_date}.json").read_bytes()
    ).hexdigest()
    ledger_hash = hashlib.sha256((paths.ledger / "paper_ledger.jsonl").read_bytes()).hexdigest()
    enabled = tuple(sorted({str(row.get("strategy_id") or "") for row in enabled_rows}))
    if not enabled or not set(source_policies).issubset(_ACCEPTED_SOURCE_POLICIES):
        raise ValueError("prior source has no exact entry-capable governed registry cohort")
    return {
        "accepted_rows": accepted_rows,
        "enabled_strategy_ids": enabled,
        "enabled_strategy_identities": {
            str(row.get("strategy_id")): {
                "strategy_version": str(row.get("strategy_version") or ""),
                "strategy_semantics_fingerprint": str(
                    row.get("strategy_semantics_fingerprint") or ""
                ),
                "execution_policy_version": str(
                    row.get("execution_policy_version") or ""
                ),
            }
            for row in enabled_rows
        },
        "source_policy_versions": sorted(source_policies),
        "provenance": {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "prior_session_date": prior_date,
            "run_ids": sorted(run_ids),
            "data_snapshot_ids": sorted(snapshots),
            "run_manifest_payload_hashes": sorted(manifest_hashes),
            "ledger_sha256": ledger_hash,
            "pick_projection_sha256": pick_bytes_hash,
            "execution_policy_version": (
                next(iter(source_policies)) if len(source_policies) == 1 else "mixed"
            ),
            "execution_policy_versions": sorted(source_policies),
            "datatruth_binding_statuses": sorted(datatruth_binding_statuses),
            "enabled_strategy_identities": {
                str(row.get("strategy_id")): {
                    "strategy_version": str(row.get("strategy_version") or ""),
                    "strategy_semantics_fingerprint": str(
                        row.get("strategy_semantics_fingerprint") or ""
                    ),
                    "execution_policy_version": str(
                        row.get("execution_policy_version") or ""
                    ),
                }
                for row in enabled_rows
            },
            "series_role": "champion",
            "accepted_decision_status": "accepted",
        },
    }


def _validate_manifest_hash(manifest: Mapping[str, Any]) -> None:
    claimed = str(manifest.get("manifest_payload_hash") or "")
    if not _SHA256.fullmatch(claimed):
        raise ValueError("run manifest payload hash is missing")
    payload = dict(manifest)
    payload.pop("manifest_payload_hash", None)
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if claimed != expected:
        raise ValueError("run manifest payload hash conflicts")


def _attest_bound_datatruth_manifest(
    paths: Any,
    run_manifest: Mapping[str, Any],
) -> str:
    """Attest a sealed DataTruth alias when immutable bytes are ACL-inaccessible.

    The normal path verifies every retained snapshot byte through
    ``_load_bound_run_dataset``.  Scheduled snapshots on some hosts expose only
    the trusted alias manifest, so a PermissionError may use this narrower
    fallback.  It validates the DataTruth manifest's own canonical hashes and
    exact run-manifest bindings, but deliberately never claims the underlying
    artifact bytes were read.
    """

    _validate_manifest_hash(run_manifest)
    if run_manifest.get("schema_version") != "v2.paper_ops_manifest.v3":
        raise ValueError("PaperOps attestation requires a v3 run manifest")
    if run_manifest.get("mode") != "forward":
        raise ValueError("PaperOps DataTruth attestation is forward-only")
    run_date = str(run_manifest.get("run_date") or "").strip()
    if not run_date:
        raise ValueError("PaperOps attestation run date is missing")
    root_binding = run_manifest.get("data_truth_root_relative")
    if not isinstance(root_binding, str) or not root_binding.strip():
        raise ValueError("PaperOps attestation DataTruth root binding is missing")
    relative_root = Path(root_binding)
    if relative_root.is_absolute():
        raise ValueError("PaperOps attestation DataTruth root binding is invalid")
    resolved_root = (Path(paths.root).resolve() / relative_root).resolve()
    expected_root = (Path(paths.root).resolve().parent / "v2_data_truth").resolve()
    if resolved_root != expected_root:
        raise ValueError("PaperOps attestation DataTruth root is noncanonical")

    snapshot_id = str(run_manifest.get("data_snapshot_id") or "").strip()
    if not snapshot_id:
        raise ValueError("PaperOps attestation snapshot identity is missing")
    alias_path = resolved_root / "manifests" / f"{snapshot_id}.json"
    payload = read_json(alias_path, None)
    if not isinstance(payload, dict):
        raise ValueError("DataTruth alias manifest is missing or malformed")
    try:
        data_manifest = _manifest_from_payload(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("DataTruth alias manifest is incomplete") from exc

    if data_manifest.schema_version != DATA_TRUTH_MANIFEST_SCHEMA_VERSION:
        raise ValueError("DataTruth alias manifest schema is unsupported")
    if data_manifest.artifact_schema_version != SNAPSHOT_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("DataTruth alias artifact schema is unsupported")
    if data_manifest.snapshot_id != snapshot_id:
        raise ValueError("DataTruth alias snapshot identity conflicts")
    if data_manifest.accepted_end != run_date:
        raise ValueError("DataTruth alias accepted end conflicts with run date")
    expected_snapshot_relative = f"snapshots/{snapshot_id}"
    if data_manifest.snapshot_relative_path != expected_snapshot_relative:
        raise ValueError("DataTruth alias snapshot path conflicts")
    expected_normalized_path = f"{expected_snapshot_relative}/normalized/ohlcv.csv"
    if data_manifest.normalized_artifact_path != expected_normalized_path:
        raise ValueError("DataTruth alias normalized path conflicts")

    if not _SHA256.fullmatch(str(data_manifest.manifest_payload_hash or "")):
        raise ValueError("DataTruth alias manifest hash is missing")
    if _manifest_payload_hash(payload) != data_manifest.manifest_payload_hash:
        raise ValueError("DataTruth alias manifest payload hash mismatch")
    if not _SHA256.fullmatch(str(data_manifest.normalized_artifact_hash or "")):
        raise ValueError("DataTruth alias normalized hash is missing")
    if not _SHA256.fullmatch(str(data_manifest.snapshot_content_hash or "")):
        raise ValueError("DataTruth alias content hash is missing")

    raw_paths = tuple(data_manifest.raw_artifact_paths)
    raw_hashes = data_manifest.raw_artifact_hashes
    if not raw_paths or len(raw_paths) != len(set(raw_paths)):
        raise ValueError("DataTruth alias raw artifact inventory is malformed")
    if set(raw_paths) != set(raw_hashes):
        raise ValueError("DataTruth alias raw artifact inventory conflicts")
    snapshot_root = Path(expected_snapshot_relative)
    logical_raw_hashes: list[tuple[str, str]] = []
    for raw_path in raw_paths:
        artifact_path = Path(raw_path)
        if artifact_path.is_absolute():
            raise ValueError("DataTruth alias raw artifact path is absolute")
        try:
            logical_path = artifact_path.relative_to(snapshot_root).as_posix()
        except ValueError as exc:
            raise ValueError("DataTruth alias raw artifact escapes its snapshot") from exc
        if (
            not logical_path
            or logical_path in {"manifest.json", "normalized/ohlcv.csv"}
            or any(part in {"", ".", ".."} for part in artifact_path.parts)
            or artifact_path.as_posix() != f"{expected_snapshot_relative}/{logical_path}"
        ):
            raise ValueError("DataTruth alias raw artifact path is invalid")
        artifact_hash = str(raw_hashes.get(raw_path) or "")
        if not _SHA256.fullmatch(artifact_hash):
            raise ValueError("DataTruth alias raw artifact hash is invalid")
        logical_raw_hashes.append((logical_path, artifact_hash))

    recomputed_content_hash = _snapshot_content_hash_from_hashes(
        provider_id=data_manifest.provider_id,
        timeframe=data_manifest.timeframe,
        symbols=data_manifest.symbols,
        requested_start=data_manifest.requested_start,
        requested_end=data_manifest.requested_end,
        accepted_start=data_manifest.accepted_start,
        accepted_end=data_manifest.accepted_end,
        normalized_hash=data_manifest.normalized_artifact_hash,
        source_artifact_hashes=tuple(logical_raw_hashes),
    )
    if data_manifest.snapshot_content_hash != recomputed_content_hash:
        raise ValueError("DataTruth alias content hash mismatch")
    if _snapshot_id(
        provider_id=data_manifest.provider_id,
        timeframe=data_manifest.timeframe,
        accepted_end=data_manifest.accepted_end,
        content_hash=recomputed_content_hash,
    ) != snapshot_id:
        raise ValueError("DataTruth alias snapshot ID is not content-bound")

    # The alias is not sufficient by itself: it must be bound to the exact
    # immutable PaperOps config that produced the inaccessible snapshot.  Read
    # the config directly rather than calling ``engine._config`` so a missing
    # config cannot trigger an init/write side effect during attestation.
    from intraday_scanner.v2.paper_ops import engine

    config_payload = read_json(paths.state / "paper_ops_config.json", None)
    if not isinstance(config_payload, dict) or not config_payload:
        raise ValueError("PaperOps attestation config is missing or malformed")
    config = engine._config_from_payload(config_payload)
    if run_manifest.get("execution_policy_version") != config.execution_policy_version:
        raise ValueError("PaperOps attestation execution policy version conflicts")
    expected_policy_fingerprint = engine._execution_policy_fingerprint(config)
    if run_manifest.get("execution_policy_fingerprint") != expected_policy_fingerprint:
        raise ValueError("PaperOps attestation execution policy fingerprint conflicts")
    raw_universe = run_manifest.get("universe_symbols")
    if (
        run_manifest.get("universe_id") != config.universe_id
        or not isinstance(raw_universe, list)
        or tuple(raw_universe) != config.universe_symbols
        or data_manifest.symbols != config.universe_symbols
    ):
        raise ValueError("PaperOps attestation DataTruth/config universe conflicts")

    run_bindings = {
        "data_snapshot_id": data_manifest.snapshot_id,
        "data_snapshot_content_hash": data_manifest.snapshot_content_hash,
        "data_snapshot_manifest_payload_hash": data_manifest.manifest_payload_hash,
        "data_snapshot_normalized_hash": data_manifest.normalized_artifact_hash,
        "data_snapshot_normalized_path": data_manifest.normalized_artifact_path,
    }
    for field, expected in run_bindings.items():
        if run_manifest.get(field) != expected:
            raise ValueError(f"PaperOps attestation {field} binding conflicts")
    return DATATRUTH_MANIFEST_ATTESTATION_LABEL


def _catalog_identities() -> dict[str, tuple[str, str]]:
    from intraday_scanner.v2.paper_ops.engine import _strategy_semantics_fingerprint
    from intraday_scanner.v2.strategies import build_strategy_catalog

    return {
        str(strategy.strategy_id): (
            str(strategy.version),
            _strategy_semantics_fingerprint(strategy),
        )
        for strategy in build_strategy_catalog()
    }


def _strategy_core_specs(strategy_id: str) -> tuple[Any, ...]:
    from intraday_scanner.decisioning.condition_registry import registry_for_strategy
    from intraday_scanner.decisioning.contracts import ConditionCategory

    return tuple(
        spec
        for spec in registry_for_strategy(strategy_id)
        if spec.category == ConditionCategory.STRATEGY_CORE
    )


def _levels_pass(row: Mapping[str, Any]) -> bool:
    entry = _number(row.get("entry_reference"))
    stop = _number(row.get("stop"))
    target = _number(row.get("target"))
    direction = str(row.get("direction") or "").lower()
    rr = _number(row.get("reward_risk"))
    if entry is None or stop is None or target is None or rr is None or entry <= 0:
        return False
    if direction == "long" and stop >= entry:
        return False
    if direction == "short" and stop <= entry:
        return False
    if direction == "long" and target <= entry:
        return False
    if direction == "short" and target >= entry:
        return False
    recomputed_rr = abs(target - entry) / abs(entry - stop)
    if rr < MIN_REWARD_RISK - 1e-12 or recomputed_rr < MIN_REWARD_RISK - 1e-12:
        return False
    if not math.isclose(rr, recomputed_rr, rel_tol=1e-9, abs_tol=1e-9):
        return False
    return abs(entry - stop) / entry <= MAX_STOP_DISTANCE_PCT + 1e-12


def _current_lane_ok(row: Mapping[str, Any]) -> bool:
    lanes = {
        str(row.get("universe_lane") or "").strip().lower(),
        str(row.get("evidence_lane") or "").strip().lower(),
    }
    if "mover+core" in lanes:
        return True
    return bool(lanes & {"mover", "core"})


def _current_observed_at(row: Mapping[str, Any]) -> str:
    value = row.get("_adapter_current_observed_at") or _source_observed_at(row)
    return str(value or "")


def _source_observed_at(row: Mapping[str, Any]) -> str:
    """Return only a timestamp tied to the actual current source payload."""

    enrichment = str(row.get("enrichment_observed_at") or "").strip()
    payload_json = str(row.get("enrichment_observation_payload_json") or "").strip()
    payload_hash = str(row.get("enrichment_observation_sha256") or "").strip().lower()
    if enrichment and payload_json and _SHA256.fullmatch(payload_hash):
        try:
            payload = json.loads(payload_json)
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
            canonical = ""
        payload_observed = (
            str(payload.get("observed_at") or "").strip()
            if isinstance(payload, dict)
            else ""
        )
        if (
            isinstance(payload, dict)
            and hashlib.sha256(canonical.encode("utf-8")).hexdigest() == payload_hash
            and payload_observed == enrichment
        ):
            return enrichment
    for key in ("source_timestamp", "source_observed_at"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    if any(
        str(row.get(key) or "").strip()
        for key in (
            "enrichment_observed_at",
            "enrichment_observation_payload_json",
            "enrichment_observation_sha256",
        )
    ):
        # An unbound enrichment timestamp cannot be replaced by a newer
        # generic clock field without masking stale/missing source truth.
        return ""
    if any(
        bool(row.get(key))
        for key in (
            "source_timestamp_is_as_of",
            "as_of_timestamp_is_source_observation",
            "as_of_is_source_timestamp",
        )
    ) or str(row.get("source_timestamp_status") or "").upper() in {
        "FRESH_BOUND",
        "SOURCE_BOUND",
        "VERIFIED_BOUND",
    }:
        return str(row.get("as_of_timestamp") or "").strip()
    return ""


def _parse_aware_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _current_freshness_limit_seconds(row: Mapping[str, Any]) -> int:
    values: list[int] = [DEFAULT_CURRENT_FRESHNESS_MAX_AGE_SECONDS]
    for key in (
        "max_age_seconds",
        "freshness_max_age_seconds",
        "premarket_enrichment_max_age_seconds",
    ):
        try:
            value = int(row.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    for key in ("freshness_receipt", "core_coverage_receipt"):
        nested = row.get(key)
        if not isinstance(nested, Mapping):
            continue
        try:
            value = int(nested.get("max_age_seconds"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return min(values)


def _validated_current_observed_at(
    row: Mapping[str, Any],
    *,
    decision_at: datetime,
    current_market_date: date,
) -> str | None:
    observed = _parse_aware_datetime(_source_observed_at(row))
    if observed is None or observed.date() > current_market_date:
        return None
    age_seconds = (decision_at - observed).total_seconds()
    if age_seconds < 0 or age_seconds > _current_freshness_limit_seconds(row):
        return None
    return observed.isoformat()


def _current_setup_open(
    row: Mapping[str, Any], *, direction: str, stop: float | None, target: float | None
) -> bool:
    current_price = _number(
        row.get("current_price")
        or row.get("premarket_price")
        or row.get("price")
        or row.get("close")
    )
    if current_price is None or stop is None or target is None:
        return False
    if direction == "long":
        return stop < current_price < target
    if direction == "short":
        return target < current_price < stop
    return False


def _prior_observed_at(row: Mapping[str, Any], prior_session_date: str) -> str:
    """Return the immutable source observation time for strategy-core proof."""

    value = (
        row.get("signal_time")
        or row.get("decision_at")
        or row.get("timestamp")
        or row.get("trade_date")
        or prior_session_date
    )
    return str(value)


def _current_fallback_blocked(row: Mapping[str, Any]) -> bool:
    """Reject current rows explicitly marked as above the data ceiling."""

    blocked_statuses = {
        "research_only_applied_above_ceiling",
        "applied_research_only_above_ceiling",
        "ceiling_exceeded_not_applied",
    }
    return bool(
        row.get("research_only_above_ceiling")
        or row.get("above_ceiling")
        or str(row.get("enrichment_fallback_status") or "").lower() in blocked_statuses
        or str(row.get("secondary_fallback_status") or "").lower() in blocked_statuses
    )


def _source_signal_id(
    market_date: str,
    ticker: str,
    strategy_id: str,
    strategy_version: str,
    source_signal_id: str,
) -> str:
    digest = hashlib.sha256(
        "|".join((market_date, ticker, strategy_id, strategy_version, source_signal_id)).encode()
    ).hexdigest()[:24]
    return f"morning-paperops:{market_date}:{ticker}:{digest}"


def _contribution_summary(
    rows: Sequence[Mapping[str, Any]], enabled_strategy_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    counts: Counter[str] = Counter(str(row.get("strategy_id") or "") for row in rows)
    identities = _catalog_identities()
    policies: dict[str, set[str]] = {}
    for row in rows:
        strategy_id = str(row.get("strategy_id") or "")
        policy = str(row.get("research_adapter_source_policy") or "").strip()
        if strategy_id and policy:
            policies.setdefault(strategy_id, set()).add(policy)
    return {
        strategy_id: {
            "strategy_id": strategy_id,
            "strategy_version": identities.get(strategy_id, ("", ""))[0],
            "strategy_semantics_fingerprint": identities.get(strategy_id, ("", ""))[1],
            "candidate_count": counts.get(strategy_id, 0),
            "enabled": True,
            "source": (
                LEGACY_SOURCE_LABEL
                if policies.get(strategy_id, {LEGACY_PAPER_EXECUTION_POLICY_VERSION})
                == {LEGACY_PAPER_EXECUTION_POLICY_VERSION}
                else GOVERNED_SOURCE_LABEL
            ),
            "source_policy_versions": sorted(policies.get(strategy_id, set())),
        }
        for strategy_id in enabled_strategy_ids
    }


def _result_base(market_date: date | None) -> dict[str, Any]:
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "status": "BLOCKED",
        "market_date": market_date.isoformat() if market_date else "",
        "prior_session_date": "",
        "rows": [],
        "adapted_count": 0,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _ticker(row: Mapping[str, Any]) -> str:
    value = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
    return value if _SYMBOL.fullmatch(value) else ""


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _valid_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _prior_session(value: date) -> date:
    current = value - timedelta(days=1)
    while not market_session(current).is_trading_day:
        current -= timedelta(days=1)
    return current


__all__ = [
    "ADAPTER_SCHEMA_VERSION",
    "DATATRUTH_MANIFEST_ATTESTATION_LABEL",
    "GOVERNED_SOURCE_LABEL",
    "LEGACY_SOURCE_LABEL",
    "adapt_prior_session_paper_ops",
    "adapt_verified_prior_session_rows",
]
