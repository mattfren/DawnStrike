"""Bounded Cycle 3 research receipts.

This module is intentionally an island beside the official AlphaOps and
PaperOps paths.  It records allocation and observation evidence; it never
places orders, writes an account, or manufactures a return from a price path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.alpha import fill_truth
from intraday_scanner.v2.paper_ops.fleet_allocator import (
    FleetAllocatorPolicy,
    FleetCandidate,
    allocate_shadow_fleet,
)
from intraday_scanner.v2.strategies import Direction

SCHEMA_VERSION = "dawnstrike.cycle3.research.v1"
REASON_CODE_VERSION = "dawnstrike.rejected-reason-codes.v1"
SAMPLING_POLICY_VERSION = "dawnstrike.rejected-ipw-session-clustered-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CODE_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_CHICAGO = ZoneInfo("America/Chicago")


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _hash(value: object, *, code: bool = False) -> str:
    text = str(value or "")
    if not (_CODE_SHA if code else _SHA256).fullmatch(text):
        kind = "code SHA" if code else "SHA-256"
        raise ValueError(f"{kind} must be canonical lowercase hexadecimal")
    return text


@dataclass(frozen=True, slots=True)
class Cycle3EvidenceHashes:
    """Immutable source/config/code/window/evidence identity for every receipt."""

    source_hash_sha256: str
    config_hash_sha256: str
    code_sha: str
    window_hash_sha256: str
    evidence_hash_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_hash_sha256", _hash(self.source_hash_sha256))
        object.__setattr__(self, "config_hash_sha256", _hash(self.config_hash_sha256))
        object.__setattr__(self, "code_sha", _hash(self.code_sha, code=True))
        object.__setattr__(self, "window_hash_sha256", _hash(self.window_hash_sha256))
        object.__setattr__(self, "evidence_hash_sha256", _hash(self.evidence_hash_sha256))

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _parse_date(value: object) -> str:
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
        if parsed.isoformat() != text:
            raise ValueError
        return parsed.isoformat()
    except ValueError as exc:
        raise ValueError("market_date must be an ISO date") from exc


def _parse_timestamp(value: object) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _market_date_from_timestamp(value: object) -> str:
    return _parse_timestamp(value).astimezone(_CHICAGO).date().isoformat()


def _finite(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be finite")
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise ValueError(f"{field} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _canonical_candidates(
    candidates: list[FleetCandidate], *, market_date: str
) -> list[FleetCandidate]:
    if not candidates:
        raise ValueError("common candidate identity must be non-empty")
    output: list[FleetCandidate] = []
    for candidate in candidates:
        candidate_id = str(candidate.candidate_id).strip()
        strategy_id = str(candidate.strategy_id).strip()
        strategy_version = str(candidate.strategy_version).strip()
        symbol = str(candidate.symbol).strip().upper()
        asset_type = str(candidate.asset_type).strip().lower()
        direction = str(candidate.direction).strip().lower()
        group = str(candidate.correlation_group).strip()
        if not candidate_id or not strategy_id or not strategy_version or not symbol:
            raise ValueError("candidate identity fields are required")
        if asset_type not in {"stock", "etf"}:
            raise ValueError("candidate asset cohort is unknown")
        if direction not in {Direction.LONG, Direction.SHORT}:
            raise ValueError("candidate direction is invalid")
        if str(candidate.individual_account_decision).strip().lower() != "accepted":
            raise ValueError("common candidates must be individually accepted")
        if not group:
            raise ValueError("correlation group is unknown")
        score = _finite(candidate.score, field="candidate score")
        risk_amount = _finite(candidate.risk_amount, field="candidate risk")
        notional = _finite(candidate.notional, field="candidate notional")
        if risk_amount <= 0 or notional <= 0:
            raise ValueError("candidate risk and notional must be positive")
        output.append(
            replace(
                candidate,
                candidate_id=candidate_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                symbol=symbol,
                asset_type=asset_type,
                direction=direction,
                score=score,
                risk_amount=risk_amount,
                notional=notional,
                correlation_group=group,
                individual_account_decision="accepted",
            )
        )
    _identity(output, market_date)
    return output


def _validate_fleet_policy(policy: FleetAllocatorPolicy) -> None:
    if (
        policy.preserve_individual_strategy_accounts is not True
        or policy.research_only is not True
        or policy.broker_execution_enabled is not False
    ):
        raise ValueError("Cycle 3 fleet policy flags are unsafe")
    for field in (
        "max_positions",
        "max_symbol_overlap",
        "max_correlation_group_positions",
    ):
        value = getattr(policy, field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"fleet policy {field} must be a positive integer")


def _identity(candidates: list[FleetCandidate], market_date: str) -> tuple[str, ...]:
    del market_date
    ids = tuple(sorted(str(candidate.candidate_id) for candidate in candidates))
    if not ids or any(not item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("common candidate identity must be non-empty and unique")
    return ids


def _fresh_truth(
    truth: Mapping[str, Any] | None,
    *,
    market_date: str,
    kind: str,
    candidate_id: str,
    correlation_group: str | None = None,
    decision_timestamp: object | None = None,
    correlation: bool = False,
) -> None:
    if not isinstance(truth, Mapping):
        raise ValueError(f"{kind} truth is missing")
    status = str(truth.get("status") or "").lower()
    if status not in {"verified", "verified_available", "complete", "current"}:
        raise ValueError(f"{kind} truth is unknown or stale")
    if str(truth.get("candidate_id") or "") != candidate_id:
        raise ValueError(f"{kind} truth candidate identity mismatch")
    if correlation and str(truth.get("correlation_group") or "") != correlation_group:
        raise ValueError("correlation truth group mismatch")
    if correlation and truth.get("correlation_value") is None:
        raise ValueError("correlation truth value is missing")
    if correlation:
        value = _finite(truth.get("correlation_value"), field="correlation truth value")
        if not -1.0 <= value <= 1.0:
            raise ValueError("correlation truth value must be between -1 and 1")
    if not truth.get("as_of"):
        raise ValueError(f"{kind} exact as-of timestamp is missing")
    observed = truth.get("as_of") or truth.get("observed_at") or truth.get("located_at")
    if not observed:
        raise ValueError(f"{kind} truth timestamp is missing")
    observed_at = _parse_timestamp(observed)
    if _market_date_from_timestamp(observed) != market_date:
        raise ValueError(f"{kind} truth is stale for market date")
    truth_decision = truth.get("decision_timestamp") or decision_timestamp
    if not truth_decision:
        raise ValueError(f"{kind} decision timestamp is missing")
    if truth.get("decision_timestamp") and decision_timestamp:
        if _parse_timestamp(truth["decision_timestamp"]) != _parse_timestamp(
            decision_timestamp
        ):
            raise ValueError(f"{kind} decision timestamp mismatch")
    decision_at = _parse_timestamp(truth_decision)
    if _market_date_from_timestamp(truth_decision) != market_date:
        raise ValueError(f"{kind} decision timestamp is not same-day")
    if observed_at > decision_at:
        raise ValueError(f"{kind} as-of is after decision timestamp")
    if not str(truth.get("source_ref") or truth.get("source") or "").strip():
        raise ValueError(f"{kind} truth source is missing")


def _candidate_truth_checks(
    candidates: list[FleetCandidate],
    *,
    market_date: str,
    correlation_truth_by_candidate: Mapping[str, Mapping[str, Any]] | None,
    decision_timestamp_by_candidate: Mapping[str, str] | None,
    borrow_truth_by_candidate: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    truth_by_id = correlation_truth_by_candidate or {}
    for candidate in candidates:
        correlation = truth_by_id.get(candidate.candidate_id)
        candidate_decision = (
            (decision_timestamp_by_candidate or {}).get(candidate.candidate_id)
            or (correlation or {}).get("decision_timestamp")
        )
        _fresh_truth(
            correlation,
            market_date=market_date,
            kind="correlation",
            candidate_id=candidate.candidate_id,
            correlation_group=candidate.correlation_group,
            decision_timestamp=candidate_decision,
            correlation=True,
        )
        if candidate.direction == Direction.SHORT:
            borrow = candidate.borrow
            if borrow is None:
                raise ValueError("short borrow truth is missing")
            borrow_truth = (borrow_truth_by_candidate or {}).get(candidate.candidate_id)
            if borrow_truth is None:
                raise ValueError("short borrow exact truth is missing")
            _fresh_truth(
                borrow_truth,
                market_date=market_date,
                kind="borrow",
                candidate_id=candidate.candidate_id,
                decision_timestamp=candidate_decision,
            )
            if (
                borrow_truth.get("status") != borrow.status
                or borrow_truth.get("borrow_cost_bps_per_session")
                != borrow.borrow_cost_bps_per_session
                or borrow_truth.get("source_ref") != borrow.source_ref
            ):
                raise ValueError("borrow truth does not match candidate snapshot")
            if not borrow.located_at or _parse_timestamp(borrow_truth["as_of"]) != _parse_timestamp(
                borrow.located_at
            ):
                raise ValueError("borrow as-of does not match candidate snapshot")
            if not borrow_truth.get("decision_timestamp"):
                raise ValueError("borrow decision timestamp is missing")
            _finite(borrow.borrow_cost_bps_per_session, field="borrow cost")


def _account_bytes(value: Mapping[str, bytes | str]) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for key, item in value.items():
        result[str(key)] = item if isinstance(item, bytes) else str(item).encode()
    return result


def _account_digests(value: Mapping[str, bytes]) -> dict[str, str]:
    return {
        key: hashlib.sha256(value[key]).hexdigest()
        for key in sorted(value)
    }


def _correlation_snapshot(
    candidates: list[FleetCandidate],
    truth_by_id: Mapping[str, Mapping[str, Any]],
    decision_timestamp_by_candidate: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": candidate.candidate_id,
            "correlation_group": candidate.correlation_group,
            "correlation_value": truth_by_id[candidate.candidate_id].get(
                "correlation_value"
            ),
            "as_of": truth_by_id[candidate.candidate_id].get("as_of")
            or truth_by_id[candidate.candidate_id].get("observed_at"),
            "decision_timestamp": truth_by_id[candidate.candidate_id].get(
                "decision_timestamp"
            )
            or (decision_timestamp_by_candidate or {}).get(candidate.candidate_id),
            "source_ref": truth_by_id[candidate.candidate_id].get("source_ref"),
        }
        for candidate in sorted(candidates, key=lambda item: item.candidate_id)
    ]


def _borrow_snapshot(
    candidates: list[FleetCandidate],
    truth_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": candidate.candidate_id,
            **{
                key: truth_by_id[candidate.candidate_id].get(key)
                for key in (
                    "status",
                    "borrow_cost_bps_per_session",
                    "as_of",
                    "decision_timestamp",
                    "source_ref",
                )
            },
        }
        for candidate in sorted(candidates, key=lambda item: item.candidate_id)
        if candidate.direction == Direction.SHORT
    ]


def build_paired_counterfactual_shadow_receipt(
    *,
    market_date: str,
    candidates: list[FleetCandidate],
    official_account_bytes: Mapping[str, bytes | str],
    post_run_official_account_bytes: Mapping[str, bytes | str],
    evidence: Cycle3EvidenceHashes,
    correlation_truth_by_candidate: Mapping[str, Mapping[str, Any]] | None = None,
    decision_timestamp_by_candidate: Mapping[str, str] | None = None,
    borrow_truth_by_candidate: Mapping[str, Mapping[str, Any]] | None = None,
    policy: FleetAllocatorPolicy | None = None,
    starting_cash: float = 0.0,
    common_candidate_ids: list[str] | None = None,
    official_candidate_ids: list[str] | None = None,
    candidate_market_date_by_id: Mapping[str, str] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, Any]:
    """Build an isolated paired fleet allocation, retaining unused cash.

    The function only accepts a same-day candidate cohort with current
    correlation truth (and current borrow truth for shorts).  ``official``
    account bytes are compared before returning so the proof is falsifiable.
    """

    day = _parse_date(market_date)
    active_policy = policy or FleetAllocatorPolicy()
    _validate_fleet_policy(active_policy)
    canonical_candidates = _canonical_candidates(candidates, market_date=day)
    ids = _identity(canonical_candidates, day)
    if common_candidate_ids is None or tuple(sorted(map(str, common_candidate_ids))) != ids:
        raise ValueError("common candidate identity does not match allocator candidates")
    if (
        official_candidate_ids is None
        or tuple(sorted(map(str, official_candidate_ids))) != ids
    ):
        raise ValueError("official candidate identity is not the common cohort")
    if candidate_market_date_by_id is None:
        raise ValueError("candidate market-date identity is missing")
    for candidate_id in ids:
        if _parse_date(candidate_market_date_by_id.get(candidate_id)) != day:
            raise ValueError("candidate identity is not same-day")
    if set(correlation_truth_by_candidate or {}) != set(ids):
        raise ValueError("correlation truth cohort does not match candidates")
    try:
        starting_cash = _finite(starting_cash, field="starting_cash")
    except ValueError as exc:
        raise ValueError("starting_cash must be finite and non-negative") from exc
    if starting_cash < 0:
        raise ValueError("starting_cash must be finite and non-negative")
    if window_start is None or window_end is None:
        raise ValueError("frozen window start and end are required")
    parsed_start = _parse_timestamp(window_start)
    parsed_end = _parse_timestamp(window_end)
    if parsed_start > parsed_end:
        raise ValueError("frozen window is reversed")
    if not (
        _market_date_from_timestamp(window_start)
        <= day
        <= _market_date_from_timestamp(window_end)
    ):
        raise ValueError("frozen window does not contain market date")
    _candidate_truth_checks(
        canonical_candidates,
        market_date=day,
        correlation_truth_by_candidate=correlation_truth_by_candidate,
        decision_timestamp_by_candidate=decision_timestamp_by_candidate,
        borrow_truth_by_candidate=borrow_truth_by_candidate,
    )
    before = _account_bytes(official_account_bytes)
    after = _account_bytes(post_run_official_account_bytes)
    if before != after:
        raise ValueError("official or individual account bytes changed")
    before_digests = _account_digests(before)
    after_digests = _account_digests(after)
    allocation = allocate_shadow_fleet(canonical_candidates, policy=active_policy)
    selected = list(allocation["selected"])
    blocked = list(allocation["blocked"])
    available_cash = float(starting_cash)
    cash_start = available_cash
    cash_selected: list[dict[str, Any]] = []
    for row in selected:
        if cash_start <= 0:
            blocked.append({**row, "fleet_decision": "BLOCKED", "reason": "cash_nonpositive"})
            continue
        notional = float(row.get("notional") or 0.0)
        if notional <= available_cash:
            available_cash -= notional
            cash_selected.append({**row, "allocated_notional": notional})
        else:
            blocked.append({**row, "fleet_decision": "BLOCKED", "reason": "cash_retained"})
    selected = cash_selected
    status = (
        "COMPLETE_RESEARCH_ONLY"
        if cash_start > 0
        else "NOT_EVALUABLE_NONPOSITIVE_CASH"
    )
    identity_hash = canonical_hash(
        {
            "status": status,
            "market_date": day,
            "candidate_ids": ids,
        }
    )
    allocation = {
        **allocation,
        "selected": selected,
        "blocked": blocked,
        "diagnostics": {
            **allocation.get("diagnostics", {}),
            "selected_count": len(selected),
            "blocked_count": len(blocked),
        },
    }
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "receipt_type": "paired_counterfactual_shadow_fleet",
        "market_date": day,
        "common_candidate_ids": list(ids),
        "common_candidate_identity_hash_sha256": identity_hash,
        "allocation": allocation,
        "cash": {
            "starting_cash": cash_start,
            "allocated_notional": round(cash_start - available_cash, 8),
            "cash_retained": round(available_cash, 8),
            "cash_retained_invariant": available_cash >= 0.0,
        },
        "account_digests": {
            "before": before_digests,
            "after": after_digests,
            "byte_identical": before_digests == after_digests,
        },
        "official_account_bytes_byte_identical": True,
        "individual_strategy_accounts_mutated": False,
        "official_paper_path_mutated": False,
        "counterfactual_return_truth": None,
        "counterfactual_return_status": "NOT_FILL_TRUTH",
        "status": status,
        "frozen_window": {
            "start": parsed_start.isoformat(),
            "end": parsed_end.isoformat(),
            "market_date_timezone": "America/Chicago",
        },
        "correlation_truth": _correlation_snapshot(
            canonical_candidates,
            correlation_truth_by_candidate or {},
            decision_timestamp_by_candidate,
        ),
        "borrow_truth": _borrow_snapshot(
            canonical_candidates, borrow_truth_by_candidate or {}
        ),
        "evidence": evidence.as_dict(),
        **evidence.as_dict(),
        "manual_review_required": True,
        "promotion_status": "MANUAL_NO_PROMOTION",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["evidence_binding_hash_sha256"] = canonical_hash(
        {
            "status": payload["status"],
            "candidate_rows": [asdict(candidate) for candidate in canonical_candidates],
            "policy": payload["allocation"]["policy"],
            "cash": payload["cash"],
            "correlation_truth": payload["correlation_truth"],
            "borrow_truth": payload["borrow_truth"],
            "frozen_window": payload["frozen_window"],
            "account_digests": payload["account_digests"],
            "evidence": payload["evidence"],
        }
    )
    payload["receipt_hash_sha256"] = canonical_hash(payload)
    return payload


class RejectedReasonCode(str, Enum):
    NOT_RANKED_BY_POLICY = "NOT_RANKED_BY_POLICY"
    SAFETY_VETO = "SAFETY_VETO"
    UNKNOWN_CORRELATION = "UNKNOWN_CORRELATION"
    STALE_CORRELATION = "STALE_CORRELATION"
    BORROW_NOT_VERIFIED = "BORROW_NOT_VERIFIED"
    OTHER_POLICY_REJECT = "OTHER_POLICY_REJECT"


def _reason_code(row: Mapping[str, Any]) -> str:
    raw = str(row.get("reason_code") or row.get("policy_rejection_reason") or "").upper()
    aliases = {
        "NOT_RANKED_BY_FROZEN_V5_CANDIDATE_POLICY": RejectedReasonCode.NOT_RANKED_BY_POLICY.value,
        "SHORT_BORROW_NOT_VERIFIED": RejectedReasonCode.BORROW_NOT_VERIFIED.value,
    }
    known = {item.value for item in RejectedReasonCode}
    return aliases.get(
        raw,
        raw if raw in known else RejectedReasonCode.OTHER_POLICY_REJECT.value,
    )


def attach_typed_rejected_sampling(
    rows: list[dict[str, Any]],
    *,
    denominator: int = 5,
    config_hash_sha256: str | None = None,
    source_hash_sha256: str | None = None,
    code_sha: str | None = None,
    window_hash_sha256: str | None = None,
    evidence_hash_sha256: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    max_weight: float = 20.0,
    min_rows: int = 2,
    min_market_sessions: int = 2,
) -> list[dict[str, Any]]:
    """Attach deterministic inclusion probability and a typed reason code."""

    if (
        isinstance(denominator, bool)
        or not isinstance(denominator, int)
        or denominator < 1
    ):
        raise ValueError("denominator must be positive")
    if denominator > 1_000_000:
        raise ValueError("denominator exceeds bounded sampling policy")
    bindings = {
        "config_hash_sha256": _hash(config_hash_sha256),
        "source_hash_sha256": _hash(source_hash_sha256),
        "code_sha": _hash(code_sha, code=True),
        "window_hash_sha256": _hash(window_hash_sha256),
        "evidence_hash_sha256": _hash(evidence_hash_sha256),
    }
    if window_start is None or window_end is None:
        raise ValueError("sampling frozen window start and end are required")
    parsed_start = _parse_timestamp(window_start)
    parsed_end = _parse_timestamp(window_end)
    if parsed_start > parsed_end:
        raise ValueError("sampling frozen window is reversed")
    max_weight = _finite(max_weight, field="sampling weight cap")
    if (
        max_weight <= 0
        or isinstance(min_rows, bool)
        or isinstance(min_market_sessions, bool)
        or not isinstance(min_rows, int)
        or not isinstance(min_market_sessions, int)
        or min_rows < 1
        or min_market_sessions < 1
    ):
        raise ValueError("sampling policy thresholds must be positive")
    policy_config = {
        "denominator": denominator,
        "max_weight": max_weight,
        "min_rows": min_rows,
        "min_market_sessions": min_market_sessions,
        "window_start": parsed_start.isoformat(),
        "window_end": parsed_end.isoformat(),
    }
    policy_config_hash = canonical_hash(policy_config)
    probability = round(1.0 / denominator, 8)
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        if row.get("action") == "SHADOW_REJECTED_POLICY":
            decision_id = str(row.get("decision_id") or "")
            if not decision_id:
                raise ValueError("rejected candidate decision_id is required")
            market_date = _parse_date(row.get("market_date"))
            if not (
                _market_date_from_timestamp(window_start)
                <= market_date
                <= _market_date_from_timestamp(window_end)
            ):
                raise ValueError("rejected candidate is outside frozen sampling window")
            included = int(canonical_hash(decision_id)[:8], 16) % denominator == 0
            attribution = {
                "status": "SAMPLED_REJECTED_CANDIDATE",
                "decision_id": decision_id,
                "market_date": market_date,
                "candidate_id": str(row.get("ticker") or row.get("candidate_id") or ""),
                "reason_code_version": REASON_CODE_VERSION,
                "reason_code": _reason_code(row),
                "sampling_policy_version": SAMPLING_POLICY_VERSION,
                "denominator": denominator,
                "included": included,
                "inclusion_probability": probability,
                "weight_method": "inverse_probability_weight",
                "frozen_window": {
                    "start": policy_config["window_start"],
                    "end": policy_config["window_end"],
                    "market_date_timezone": "America/Chicago",
                },
                "sampling_policy_config": policy_config,
                "sampling_policy_config_hash_sha256": policy_config_hash,
                **bindings,
            }
            attribution["sampling_receipt_hash_sha256"] = canonical_hash(attribution)
            row["rejected_attribution"] = attribution
            row["reason_code"] = attribution["reason_code"]
            row["inclusion_probability"] = probability
        output.append(row)
    return output


def _validate_authenticated_outcome(
    attribution: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    payload = attribution.get("authenticated_outcome")
    if not isinstance(payload, Mapping):
        return None, "missing_authenticated_nested_outcome"
    if not fill_truth.has_authenticated_committed_fill_truth(payload):
        return None, "fill_truth_not_authenticated"
    required = (
        "decision_id",
        "candidate_id",
        "market_date",
        "research_only",
        "broker_execution_enabled",
        "config_hash_sha256",
        "source_hash_sha256",
        "code_sha",
        "window_hash_sha256",
        "evidence_hash_sha256",
        "sampling_policy_config_hash_sha256",
        "frozen_window",
        "return_payload_hash_sha256",
    )
    if any(key not in payload for key in required):
        return None, "authenticated_outcome_lineage_missing"
    if (
        payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
    ):
        return None, "authenticated_outcome_flags_invalid"
    if str(payload.get("decision_id")) != str(attribution.get("decision_id")):
        return None, "authenticated_outcome_decision_mismatch"
    if str(payload.get("candidate_id")) != str(attribution.get("candidate_id")):
        return None, "authenticated_outcome_candidate_mismatch"
    try:
        if _parse_date(payload.get("market_date")) != _parse_date(
            attribution.get("market_date")
        ):
            return None, "authenticated_outcome_date_mismatch"
    except ValueError:
        return None, "authenticated_outcome_date_invalid"
    for key in (
        "config_hash_sha256",
        "source_hash_sha256",
        "code_sha",
        "window_hash_sha256",
        "evidence_hash_sha256",
    ):
        try:
            expected = _hash(attribution.get(key), code=key == "code_sha")
            actual = _hash(payload.get(key), code=key == "code_sha")
        except ValueError:
            return None, "authenticated_outcome_hash_invalid"
        if expected != actual:
            return None, f"authenticated_outcome_{key}_mismatch"
    if payload.get("sampling_policy_config_hash_sha256") != attribution.get(
        "sampling_policy_config_hash_sha256"
    ):
        return None, "authenticated_outcome_sampling_policy_mismatch"
    if payload.get("frozen_window") != attribution.get("frozen_window"):
        return None, "authenticated_outcome_window_contract_mismatch"
    supplied_payload_hash = payload.get("return_payload_hash_sha256")
    computed_payload_hash = canonical_hash(
        {key: value for key, value in payload.items() if key != "return_payload_hash_sha256"}
    )
    if supplied_payload_hash != computed_payload_hash:
        return None, "authenticated_outcome_payload_hash_mismatch"
    try:
        value = payload.get("outcome_value")
        if value is None:
            value = payload.get("after_cost_return_pct")
        _finite(value, field="authenticated outcome")
    except ValueError:
        return None, "authenticated_outcome_numeric_value_missing"
    window = attribution.get("frozen_window")
    if not isinstance(window, Mapping) or not window.get("start") or not window.get("end"):
        return None, "sampling_frozen_window_missing"
    try:
        inside_window = (
            _market_date_from_timestamp(window["start"])
            <= _parse_date(payload.get("market_date"))
            <= _market_date_from_timestamp(window["end"])
        )
    except ValueError:
        return None, "authenticated_outcome_window_invalid"
    if not inside_window:
        return None, "authenticated_outcome_outside_frozen_window"
    return dict(payload), None


def _attribution_receipt(body: dict[str, Any]) -> dict[str, Any]:
    body["receipt_hash_sha256"] = canonical_hash(body)
    return body


def _attribution_input_set_hash(rows: list[Mapping[str, Any]]) -> str:
    """Hash every nested sampling receipt, independent of caller row fields.

    The nested receipt is the only attribution identity.  Sorting by each
    receipt's canonical hash makes the input identity invariant to row order,
    while retaining duplicate receipts and included/excluded receipts alike.
    In particular, convenience fields such as a caller-supplied ``outcome``
    never enter this hash.
    """

    nested_receipts = [
        dict(nested)
        for row in rows
        if isinstance((nested := row.get("rejected_attribution")), Mapping)
    ]
    nested_receipts.sort(key=canonical_hash)
    return canonical_hash(nested_receipts)


def evaluate_rejected_candidate_attribution(
    rows: list[Mapping[str, Any]],
    *,
    evidence: Cycle3EvidenceHashes,
    max_weight: float = 20.0,
    min_rows: int = 2,
    min_market_sessions: int = 2,
) -> dict[str, Any]:
    """Estimate sampled rejected outcomes with capped IPW and session clusters."""

    try:
        max_weight = float(max_weight)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_weight must be positive") from exc
    if not math.isfinite(max_weight) or max_weight <= 0:
        raise ValueError("max_weight must be positive")
    if (
        isinstance(min_rows, bool)
        or isinstance(min_market_sessions, bool)
        or not isinstance(min_rows, int)
        or not isinstance(min_market_sessions, int)
        or min_rows < 1
        or min_market_sessions < 1
    ):
        raise ValueError("minimum evidence thresholds must be positive")
    sampled: list[Mapping[str, Any]] = []
    input_attribution_set_hash = _attribution_input_set_hash(rows)
    input_denominators: list[int] = []
    input_policy_configs: list[str] = []
    input_policy_config_payloads: list[dict[str, Any]] = []
    for row in rows:
        attribution = row.get("rejected_attribution")
        if not isinstance(attribution, Mapping) or (
            "sampling_receipt_hash_sha256" not in attribution
        ):
            continue
        if not isinstance(attribution, Mapping):
            raise ValueError("rejected attribution sampling receipt is missing")
        sampling_hash = attribution.get("sampling_receipt_hash_sha256")
        if sampling_hash != canonical_hash(
            {
                key: value
                for key, value in attribution.items()
                if key != "sampling_receipt_hash_sha256"
            }
        ):
            raise ValueError("rejected attribution sampling receipt hash mismatch")
        denominator = attribution.get("denominator")
        if (
            isinstance(denominator, bool)
            or not isinstance(denominator, int)
            or denominator < 1
            or denominator > 1_000_000
        ):
            raise ValueError("rejected attribution denominator is invalid")
        policy_config = attribution.get("sampling_policy_config")
        policy_config_hash = attribution.get("sampling_policy_config_hash_sha256")
        if (
            not isinstance(policy_config, Mapping)
            or policy_config_hash != canonical_hash(policy_config)
        ):
            raise ValueError("rejected attribution policy configuration hash mismatch")
        decision_id = str(attribution.get("decision_id") or "")
        if not decision_id or attribution.get("status") != "SAMPLED_REJECTED_CANDIDATE":
            raise ValueError("rejected attribution sampling identity is invalid")
        expected_included = int(canonical_hash(decision_id)[:8], 16) % denominator == 0
        if attribution.get("included") is not expected_included:
            raise ValueError("rejected attribution deterministic sampling mismatch")
        row_bindings = {
            key: _hash(attribution.get(key), code=key == "code_sha")
            for key in (
                "config_hash_sha256",
                "source_hash_sha256",
                "code_sha",
                "window_hash_sha256",
                "evidence_hash_sha256",
            )
        }
        if row_bindings != evidence.as_dict():
            raise ValueError("rejected attribution evidence bindings disagree")
        input_denominators.append(denominator)
        input_policy_configs.append(str(policy_config_hash))
        input_policy_config_payloads.append(dict(policy_config))
    for row in rows:
        nested = row.get("rejected_attribution")
        # Inclusion is authoritative only in the nested deterministic object;
        # a caller cannot override it with a top-level convenience field.
        if isinstance(nested, Mapping) and nested.get("included") is True:
            sampled.append(row)
    weighted: list[tuple[str, float, float]] = []
    missing_probability = False
    missing_outcome = False
    invalid_date = False
    bindings: dict[str, str] | None = evidence.as_dict()
    sampling_hashes: list[str] = []
    outcome_hashes: list[str] = []
    policy_configs: list[str] = []
    policy_config_payloads: list[dict[str, Any]] = []
    reasons: list[str] = []
    denominators: list[int] = []
    for row in sampled:
        attribution = (
            row.get("rejected_attribution")
            if isinstance(row.get("rejected_attribution"), Mapping)
            else row
        )
        if not isinstance(attribution, Mapping):
            raise ValueError("rejected attribution sampling receipt is missing")
        sampling_hash = attribution.get("sampling_receipt_hash_sha256")
        if (
            not isinstance(attribution, Mapping)
            or not sampling_hash
            or sampling_hash != canonical_hash(
                {
                    key: value
                    for key, value in attribution.items()
                    if key != "sampling_receipt_hash_sha256"
                }
            )
        ):
            raise ValueError("rejected attribution sampling receipt hash mismatch")
        denominator = attribution.get("denominator")
        decision_id = str(attribution.get("decision_id") or "")
        reason = str(attribution.get("reason_code") or "")
        if denominator is None:
            raise ValueError("rejected attribution denominator is invalid")
        try:
            denominator_int = int(denominator)
        except (TypeError, ValueError) as exc:
            raise ValueError("rejected attribution denominator is invalid") from exc
        if (
            attribution.get("status") != "SAMPLED_REJECTED_CANDIDATE"
            or attribution.get("sampling_policy_version") != SAMPLING_POLICY_VERSION
            or reason not in {item.value for item in RejectedReasonCode}
            or isinstance(denominator, bool)
            or denominator_int < 1
            or denominator_int > 1_000_000
            or not decision_id
            or (int(canonical_hash(decision_id)[:8], 16) % denominator_int == 0)
            != (attribution.get("included") is True)
        ):
            raise ValueError("rejected attribution deterministic sampling mismatch")
        denominators.append(denominator_int)
        sampling_hashes.append(str(sampling_hash))
        reasons.append(reason)
        policy_configs.append(str(attribution.get("sampling_policy_config_hash_sha256") or ""))
        policy_config = attribution.get("sampling_policy_config")
        if not isinstance(policy_config, Mapping):
            raise ValueError("rejected attribution policy configuration is missing")
        if attribution.get("sampling_policy_config_hash_sha256") != canonical_hash(
            policy_config
        ):
            raise ValueError("rejected attribution policy configuration hash mismatch")
        policy_config_payloads.append(dict(policy_config))
        row_bindings = {
            key: _hash(attribution.get(key), code=key == "code_sha")
            for key in (
                "config_hash_sha256",
                "source_hash_sha256",
                "code_sha",
                "window_hash_sha256",
                "evidence_hash_sha256",
            )
        }
        if bindings is None:
            bindings = row_bindings
        elif bindings != row_bindings:
            raise ValueError("rejected attribution evidence bindings disagree")
        probability = attribution.get("inclusion_probability")
        outcome_receipt, _outcome_error = _validate_authenticated_outcome(attribution)
        if outcome_receipt is None:
            missing_outcome = True
            continue
        outcome = (
            outcome_receipt.get("outcome_value")
            if isinstance(outcome_receipt, Mapping)
            else None
        )
        if outcome is None and isinstance(outcome_receipt, Mapping):
            outcome = outcome_receipt.get("after_cost_return_pct")
        outcome_hashes.append(str(outcome_receipt["return_payload_hash_sha256"]))
        try:
            market_date = _parse_date(attribution.get("market_date"))
        except ValueError:
            invalid_date = True
            continue
        if probability is None:
            missing_probability = True
            continue
        if outcome is None:
            missing_outcome = True
            continue
        try:
            p = float(probability)
            y = float(outcome)
        except (TypeError, ValueError):
            missing_probability |= probability is None or probability == ""
            missing_outcome |= outcome is None or outcome == ""
            continue
        if not (0.0 < p <= 1.0) or not math.isfinite(y):
            missing_probability |= not (0.0 < p <= 1.0)
            missing_outcome |= not math.isfinite(y)
            continue
        if abs(p - (1.0 / denominator_int)) > 1e-9:
            missing_probability = True
            continue
        weight = 1.0 / p
        weighted.append(
            (
                market_date,
                min(weight, max_weight),
                y,
            )
        )
    sessions = {session for session, _, _ in weighted}
    if len(set(input_denominators)) > 1 or len(set(input_policy_configs)) > 1:
        raise ValueError("rejected attribution sampling policy is mixed")
    if input_policy_config_payloads:
        configured_cap = _finite(
            input_policy_config_payloads[0].get("max_weight"),
            field="sampling weight cap",
        )
        if configured_cap != max_weight:
            raise ValueError("sampling weight cap differs from frozen policy")
        configured = input_policy_config_payloads[0]
        if (
            configured.get("min_rows") != min_rows
            or configured.get("min_market_sessions") != min_market_sessions
            or configured.get("denominator") != input_denominators[0]
        ):
            raise ValueError("sampling thresholds differ from frozen policy")
    common_body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "NOT_EVALUABLE_MISSING_OR_INSUFFICIENT_EVIDENCE",
        "reason": (
            "missing_or_invalid_authenticated_outcome"
            if missing_outcome
            else "missing_or_invalid_inclusion_probability"
            if missing_probability
            else "invalid_market_date"
            if invalid_date
            else "minimum_rows_or_market_sessions_not_met"
        ),
        "reason_codes": sorted(set(reasons)),
        "estimand": None,
        "input_attribution_set_hash_sha256": input_attribution_set_hash,
        "sampling_receipt_hashes": sorted(set(sampling_hashes)),
        "outcome_payload_hashes": sorted(set(outcome_hashes)),
        "evidence_bindings": bindings,
        "sampling_policy_config_hashes": sorted(set(input_policy_configs)),
        "sampling_policy_configs": sorted(
            input_policy_config_payloads,
            key=lambda item: canonical_hash(item),
        ),
        "denominators": sorted(set(input_denominators)),
        "minimum_rows": min_rows,
        "minimum_market_sessions": min_market_sessions,
        "weight_cap": max_weight,
        "weighted_rows": len(weighted),
        "market_sessions": len(sessions),
        "capped_weight_count": sum(
            1 for _, weight, _ in weighted if weight >= max_weight
        ),
        "effective_sample_size": None,
        "clustered_by_session": None,
        "missing_inclusion_probability": missing_probability,
        "missing_outcome_truth": missing_outcome,
        "invalid_market_date": invalid_date,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if (
        missing_probability
        or missing_outcome
        or invalid_date
        or len(weighted) < min_rows
        or len(sessions) < min_market_sessions
    ):
        return _attribution_receipt(common_body)
    weights = [weight for _, weight, _ in weighted]
    values = [value for _, _, value in weighted]
    total_weight = sum(weights)
    estimate = sum(
        weight * value for weight, value in zip(weights, values, strict=True)
    ) / total_weight
    ess = total_weight * total_weight / sum(weight * weight for weight in weights)
    clusters: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for session, weight, value in weighted:
        clusters[session].append((weight, value))
    cluster_means = [
        sum(weight * value for weight, value in rows_for_session)
        / sum(weight for weight, _ in rows_for_session)
        for rows_for_session in clusters.values()
    ]
    cluster_se = (
        math.sqrt(
            sum((value - estimate) ** 2 for value in cluster_means)
            / (len(cluster_means) * (len(cluster_means) - 1))
        )
        if len(cluster_means) > 1
        else None
    )
    result = {
        **common_body,
        "status": "EVALUABLE_RESEARCH_ONLY",
        "estimand": estimate,
        "capped_weight_count": sum(1 for weight in weights if weight >= max_weight),
        "effective_sample_size": ess,
        "clustered_by_session": {
            "cluster_count": len(cluster_means),
            "standard_error": cluster_se,
            "interval_95": (
                [estimate - 1.96 * cluster_se, estimate + 1.96 * cluster_se]
                if cluster_se is not None
                else None
            ),
        },
        "weight_method": "capped_inverse_probability_weight",
    }
    return _attribution_receipt(result)


def build_scenario_prefilter_observation_receipt(
    *,
    market_date: str,
    observations: list[Mapping[str, Any]],
    evidence: Cycle3EvidenceHashes,
    scenario_config_hash_sha256: str,
    observation_policy_config: Mapping[str, Any] | None = None,
    observation_policy_config_hash_sha256: str | None = None,
) -> dict[str, Any]:
    """Record prospective Scenario prefilter/non-trade observations.

    Opportunity labels remain null until the private trusted FillTruth
    boundary authenticates a closed paper outcome.  A counterfactual
    open-to-close path is never accepted as FillTruth here.
    """

    day = _parse_date(market_date)
    config_hash = _hash(scenario_config_hash_sha256)
    if config_hash != evidence.config_hash_sha256:
        raise ValueError("Scenario config hash does not match evidence binding")
    if observation_policy_config is None or observation_policy_config_hash_sha256 is None:
        raise ValueError("Scenario frozen observation-policy configuration is required")
    policy_config = dict(observation_policy_config)
    policy_config_hash = _hash(observation_policy_config_hash_sha256)
    if policy_config_hash != canonical_hash(policy_config):
        raise ValueError("Scenario observation-policy configuration hash mismatch")
    rows: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    seen_decisions: set[str] = set()
    policy_names: set[str] = set()
    for source in observations:
        row = dict(source)
        candidate_id = str(row.get("candidate_id") or row.get("scenario_decision_id") or "")
        if not candidate_id:
            raise ValueError("Scenario observation candidate identity is required")
        decision_id = str(row.get("decision_id") or "").strip()
        observation_policy = str(
            row.get("observation_policy") or row.get("policy_version") or ""
        ).strip()
        if not decision_id or not observation_policy:
            raise ValueError("Scenario observation decision identity and policy are required")
        if candidate_id in seen_candidates or decision_id in seen_decisions:
            raise ValueError("Scenario observation has duplicate/conflicting identity")
        seen_candidates.add(candidate_id)
        seen_decisions.add(decision_id)
        policy_names.add(observation_policy)
        decision_at = row.get("decision_at")
        if not decision_at or _market_date_from_timestamp(decision_at) != day:
            raise ValueError("Scenario decision timestamp is missing or not same-day")
        prefilter_decision = str(row.get("prefilter_decision") or "NOT_TRADE").upper()
        if prefilter_decision not in {
            "NOT_TRADE",
            "PREFILTER_REJECTED",
            "OBSERVE_ONLY",
            # Scenario Intelligence statuses are prospective observations,
            # never executable actions.  ENTER_LONG is deliberately absent.
            "PREFILTERED",
            "ABSTAIN",
            "WATCH",
            "AVOID",
        }:
            raise ValueError("Scenario prefilter receipt only accepts non-trade observations")
        if str(row.get("action") or "").upper() == "ENTER_LONG":
            raise ValueError("Scenario prefilter receipt cannot contain ENTER_LONG")
        observation_date = _parse_date(row.get("market_date") or day)
        if observation_date != day:
            raise ValueError("Scenario observation is not same-day")
        closed_outcome = row.get("closed_paper_outcome")
        trusted = fill_truth.has_authenticated_committed_fill_truth(closed_outcome)
        closed_decision_at = (
            closed_outcome.get("decision_at") or closed_outcome.get("decision_timestamp")
            if isinstance(closed_outcome, Mapping)
            else None
        )
        try:
            private_hashes_match = (
                isinstance(closed_outcome, Mapping)
                and closed_outcome.get("config_hash_sha256") == evidence.config_hash_sha256
                and closed_outcome.get("source_hash_sha256") == evidence.source_hash_sha256
                and closed_outcome.get("code_sha") == evidence.code_sha
                and closed_outcome.get("window_hash_sha256") == evidence.window_hash_sha256
                and closed_outcome.get("evidence_hash_sha256") == evidence.evidence_hash_sha256
                and closed_outcome.get("observation_policy_config_hash_sha256")
                == policy_config_hash
                and closed_outcome.get("return_payload_hash_sha256")
                == canonical_hash(
                    {
                        key: value
                        for key, value in closed_outcome.items()
                        if key != "return_payload_hash_sha256"
                    }
                )
            )
            exact_private_evidence = (
                trusted
                and isinstance(closed_outcome, Mapping)
                and str(closed_outcome.get("candidate_id") or "") == candidate_id
                and str(closed_outcome.get("decision_id") or "") == decision_id
                and str(closed_outcome.get("observation_policy") or "") == observation_policy
                and closed_decision_at
                and _parse_timestamp(closed_decision_at) == _parse_timestamp(decision_at)
                and _parse_date(closed_outcome.get("market_date")) == day
                and closed_outcome.get("evidence_hash_sha256") == evidence.evidence_hash_sha256
                and str(closed_outcome.get("outcome_status") or "").upper()
                == "COMPLETE_SOURCED"
                and closed_outcome.get("research_only") is True
                and closed_outcome.get("broker_execution_enabled") is False
                and private_hashes_match
            )
        except (TypeError, ValueError):
            private_hashes_match = False
            exact_private_evidence = False
        outcome_status = (
            "AUTHENTICATED_CLOSED_PAPER"
            if exact_private_evidence
            else "OUTCOME_TRUTH_PENDING"
        )
        # This contract deliberately exposes only a binary research label, not
        # a return or P&L.  The trusted adapter owns whether an outcome is good.
        opportunity = (
            closed_outcome.get("opportunity_label")
            if exact_private_evidence and isinstance(closed_outcome, Mapping)
            else None
        )
        if opportunity is not None and opportunity not in {
            "positive",
            "negative",
            "not_opportunity",
        }:
            opportunity = None
        rows.append({
            "candidate_id": candidate_id,
            "decision_id": decision_id,
            "decision_at": _parse_timestamp(decision_at).isoformat(),
            "observation_policy": observation_policy,
            "observation_policy_config": policy_config,
            "observation_policy_config_hash_sha256": policy_config_hash,
            "market_date": day,
            "prefilter_decision": prefilter_decision,
            "non_trade_observation": True,
            "outcome_status": outcome_status,
            "false_negative_opportunity_label": opportunity,
            "return_pct": None,
            "official_pnl": None,
            "counterfactual_open_to_close": None,
            "fill_truth_required": True,
            "evidence": evidence.as_dict(),
            "observation_identity_hash_sha256": canonical_hash(
                {
                    "status": outcome_status,
                    "candidate_id": candidate_id,
                    "decision_id": decision_id,
                    "decision_at": _parse_timestamp(decision_at).isoformat(),
                    "market_date": day,
                    "observation_policy": observation_policy,
                    "observation_policy_config_hash_sha256": policy_config_hash,
                    "evidence": evidence.as_dict(),
                }
            ),
            "research_only": True,
            "broker_execution_enabled": False,
        })
    if len(policy_names) > 1:
        raise ValueError("Scenario observations use mixed frozen policies")
    rows.sort(key=lambda row: (str(row["candidate_id"]), str(row["decision_id"])))
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "receipt_type": "scenario_prefilter_non_trade_observation",
        "status": "COMPLETE_RESEARCH_ONLY",
        "market_date": day,
        "scenario_config_hash_sha256": config_hash,
        "observation_policy_config": policy_config,
        "observation_policy_config_hash_sha256": policy_config_hash,
        "observations": rows,
        "trusted_closed_paper_outcome_count": sum(
            row["outcome_status"] == "AUTHENTICATED_CLOSED_PAPER" for row in rows
        ),
        "false_negative_calibration_status": (
            "EVALUABLE_AFTER_TRUSTED_FILL_TRUTH"
            if any(row["false_negative_opportunity_label"] is not None for row in rows)
            else "NOT_EVALUABLE_FILL_TRUTH_REQUIRED"
        ),
        "no_public_callback_or_capability_override": True,
        "automatic_policy_mutation": False,
        "official_pnl": None,
        "evidence": evidence.as_dict(),
        **evidence.as_dict(),
        "manual_review_required": True,
        "promotion_status": "MANUAL_NO_PROMOTION",
        "research_only": True,
        "broker_execution_enabled": False,
        "missing_truth_is_zero": False,
    }
    payload["receipt_hash_sha256"] = canonical_hash(payload)
    return payload


def validate_cycle3_receipt(receipt: Mapping[str, Any]) -> bool:
    """Validate the immutable outer hash without treating it as FillTruth."""

    supplied = receipt.get("receipt_hash_sha256")
    if not isinstance(supplied, str):
        return False
    return supplied == canonical_hash(
        {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    )


__all__ = [
    "Cycle3EvidenceHashes",
    "RejectedReasonCode",
    "SCHEMA_VERSION",
    "SAMPLING_POLICY_VERSION",
    "attach_typed_rejected_sampling",
    "build_paired_counterfactual_shadow_receipt",
    "build_scenario_prefilter_observation_receipt",
    "canonical_hash",
    "evaluate_rejected_candidate_attribution",
    "validate_cycle3_receipt",
]
