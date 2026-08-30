"""Private, immutable queue for hash-bound daily remediation proposals.

The daily learner deliberately commits a receipt and proposal artifact as two
files.  Joining those files into the existing commit manifest would require
changing that manifest's contract (and its first-writer-wins protocol), so
this module exposes a deterministic entry point and an independent immutable
queue artifact.  It never changes the daily artifacts, policy, champion, or
broker state.

Only ``PROPOSED_NOT_APPLIED`` proposals from a matching, hash-valid daily
receipt are accepted.  This is an evidence inventory, not a return model:
caller prose and claimed returns are ignored for ranking and no proposal can
be applied or promoted by this service.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from intraday_scanner.market_calendar import US_MARKET_HOLIDAYS

QUEUE_SCHEMA = "dawnstrike.managed_strategy_learning_queue.v1"
POLICY_SCHEMA = "dawnstrike.managed_strategy_learning_policy.v1"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFETY_FIELDS = {
    "research_only",
    "broker_execution_enabled",
    "automatic_policy_change",
    "automatic_promotion",
    "applied",
    "champion_mutated",
    "missing_outcomes_are_zero",
}
_COUNT_FIELDS = {
    "sample_count",
    "session_count",
    "eligible_sample_count",
    "eligible_session_count",
    "evidence_sample_count",
    "evidence_session_count",
    "supporting_miss_count",
    "minimum_sample_count",
    "minimum_session_count",
}
_EVIDENCE_ID_FIELDS = (
    "evidence_identity",
    "evidence_id",
    "evidence_hash_sha256",
    "evidence_hash",
    "evidence_cohort_hash_sha256",
    "cohort_hash_sha256",
    "cohort_hash",
    "sample_hash_sha256",
    "session_hash_sha256",
)
_RETURN_CLAIM_RE = re.compile(
    r"(?:return|pnl|profit|roi|win.?rate|r.?multiple|expect(?:ed)?_?value)", re.I
)


class LearningQueueValidationError(ValueError):
    """Raised when untrusted or inconsistent learning input is rejected."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise LearningQueueValidationError("input is not canonical JSON") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reject_non_finite(value: Any, *, path: str = "input") -> None:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise LearningQueueValidationError(f"{path} contains NaN or Infinity")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_non_finite(child, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _reject_non_finite(child, path=f"{path}[{index}]")


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise LearningQueueValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _iso_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LearningQueueValidationError(f"{field} must be an ISO market date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise LearningQueueValidationError(f"{field} must be an ISO market date") from exc
    if parsed.isoformat() != value:
        raise LearningQueueValidationError(f"{field} must use canonical YYYY-MM-DD")
    return value


def _nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LearningQueueValidationError(f"{field} must be a non-negative integer or null")
    return value


def _read_json(value: Mapping[str, Any] | str | Path, field: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
    else:
        if Path(value).is_symlink():
            raise LearningQueueValidationError(f"{field} path must not be a symlink")
        try:
            result = json.loads(Path(value).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LearningQueueValidationError(f"{field} is not readable JSON") from exc
    if not isinstance(result, dict):
        raise LearningQueueValidationError(f"{field} must be an object")
    _reject_non_finite(result, path=field)
    return result


def _artifact_body(payload: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != hash_field}


def _validate_artifact_hash(payload: Mapping[str, Any], hash_field: str) -> str:
    digest = _hash(payload.get(hash_field), hash_field)
    expected = _sha256(_artifact_body(payload, hash_field))
    if digest != expected:
        raise LearningQueueValidationError(f"{hash_field} does not match canonical artifact")
    return digest


def _safety(payload: Mapping[str, Any], *, label: str) -> None:
    required = {
        "research_only": True,
        "broker_execution_enabled": False,
        "automatic_policy_change": False,
        "automatic_promotion": False,
    }
    for field, expected in required.items():
        if payload.get(field) is not expected:
            raise LearningQueueValidationError(f"{label}.{field} violates research-only safety")
    for field in ("champion_mutated", "missing_outcomes_are_zero"):
        if field in payload and payload[field] is not False:
            raise LearningQueueValidationError(f"{label}.{field} violates research-only safety")
    forbidden_true = {
        "public",
        "public_artifact",
        "published",
        "publication_enabled",
        "broker_enabled",
        "execution_enabled",
        "live_trading",
    }
    for field, value in payload.items():
        if str(field).lower() in forbidden_true and value is True:
            raise LearningQueueValidationError(f"{label}.{field} is outside the private queue")


def _lineage(payload: Mapping[str, Any], *, label: str) -> dict[str, str]:
    aliases = {
        "source_hash_sha256": ("source_hash_sha256", "source_hash"),
        "input_hash_sha256": ("input_hash_sha256", "input_hash"),
        "config_hash_sha256": ("config_hash_sha256", "configuration_hash_sha256"),
        "code_sha": ("code_sha", "code_hash_sha256", "code_sha256"),
        "window_hash_sha256": ("window_hash_sha256", "window_hash"),
    }
    values: dict[str, str] = {}
    for canonical, names in aliases.items():
        value = next((payload.get(name) for name in names if payload.get(name) is not None), None)
        if canonical == "code_sha":
            if not isinstance(value, str) or not value.strip():
                raise LearningQueueValidationError(f"{label}.code_sha is required")
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value.strip()):
                raise LearningQueueValidationError(f"{label}.code_sha must be a git/SHA-256 digest")
            values[canonical] = value.strip()
        elif value is None and canonical == "config_hash_sha256":
            # The current daily receipt has no separate runtime-config file;
            # its immutable strategy catalog is the frozen config boundary.
            catalog = payload.get("catalog")
            if catalog is None:
                raise LearningQueueValidationError(f"{label}.config_hash_sha256 is required")
            values[canonical] = _sha256({"catalog": catalog})
        elif value is None and canonical == "window_hash_sha256":
            market_date = payload.get("market_date")
            cutoff = payload.get("cutoff")
            if market_date is None or cutoff is None:
                raise LearningQueueValidationError(f"{label}.window_hash_sha256 is required")
            values[canonical] = _sha256({"market_date": market_date, "cutoff": cutoff})
        else:
            values[canonical] = _hash(value, f"{label}.{canonical}")
    return values


def _proposal_reason(proposal: Mapping[str, Any]) -> dict[str, Any]:
    reason = proposal.get("reason")
    if reason is None:
        reason = proposal.get("reason_type") or proposal.get("reason_code")
    if reason is None:
        reason = proposal.get("root_cause_category") or proposal.get("hypothesis_id")
    if isinstance(reason, Mapping):
        reason_type = reason.get("type") or reason.get("code") or reason.get("category")
        if not isinstance(reason_type, str) or not reason_type.strip():
            raise LearningQueueValidationError("proposal reason must have a typed code")
        return {"type": reason_type.strip(), "value": dict(reason)}
    if not isinstance(reason, str) or not reason.strip():
        raise LearningQueueValidationError("proposal reason identity is required")
    return {"type": "category", "value": reason.strip()}


def _proposal_variable(proposal: Mapping[str, Any]) -> Any:
    value = proposal.get("proposed_variable")
    if value is None:
        value = proposal.get("proposed_variable_identity")
    if value is None:
        controlled = proposal.get("controlled_change")
        if isinstance(controlled, Mapping):
            value = {
                key: controlled[key]
                for key in ("scope", "component", "variable", "name")
                if key in controlled
            }
    if value is None:
        raise LearningQueueValidationError("proposal proposed-variable identity is required")
    if isinstance(value, str) and not value.strip():
        raise LearningQueueValidationError("proposal proposed-variable identity is required")
    if isinstance(value, (Mapping, Sequence)) and not value:
        raise LearningQueueValidationError("proposal proposed-variable identity is required")
    return value


@dataclass(frozen=True)
class LearningQueuePolicy:
    """Frozen evidence/EVI policy; all values are persisted in queue output."""

    policy_id: str = "evidence_evi_v1"
    minimum_sample_count: int = 30
    minimum_session_count: int = 10
    validation_lag_sessions: int = 1
    recurrence_weight_bps: int = 200
    sample_weight_bps: int = 450
    session_weight_bps: int = 350

    def __post_init__(self) -> None:
        for field in (
            "minimum_sample_count",
            "minimum_session_count",
            "validation_lag_sessions",
            "recurrence_weight_bps",
            "sample_weight_bps",
            "session_weight_bps",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LearningQueueValidationError(f"policy {field} must be a non-negative integer")
        if self.sample_weight_bps + self.session_weight_bps + self.recurrence_weight_bps != 1000:
            raise LearningQueueValidationError("policy EVI weights must sum to 1000 basis points")
        if not self.policy_id.strip():
            raise LearningQueueValidationError("policy_id is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA,
            "policy_id": self.policy_id,
            "minimum_sample_count": self.minimum_sample_count,
            "minimum_session_count": self.minimum_session_count,
            "validation_lag_sessions": self.validation_lag_sessions,
            "evi": {
                "sample_weight_bps": self.sample_weight_bps,
                "session_weight_bps": self.session_weight_bps,
                "recurrence_weight_bps": self.recurrence_weight_bps,
                "formula": (
                    "floor(sample_fraction*sample_weight_bps + "
                    "session_fraction*session_weight_bps + "
                    "recurrence_fraction*recurrence_weight_bps)"
                ),
                "tie_break": ["evi_score_bps_desc", "queue_item_id_asc"],
                "claimed_returns_used": False,
                "caller_prose_used": False,
            },
        }


DEFAULT_POLICY = LearningQueuePolicy()


def _calendar_details(
    calendar: Sequence[str] | Mapping[str, Any] | None,
) -> tuple[tuple[str, ...], str | None, str | None] | None:
    """Return a validated authoritative calendar, or ``None`` when absent.

    A weekday fallback is deliberately not permitted: this queue must never
    turn a missing exchange-session source into an invented validation date.
    Mapping calendars carry their own canonical digest and identity.  A bare
    sequence is accepted as an absent/untrusted calendar for compatibility,
    but cannot make an item evaluable.
    """

    if calendar is None or not isinstance(calendar, Mapping):
        return None
    raw = calendar.get("market_dates")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        return None
    hash_field = next(
        (
            name
            for name in ("calendar_hash_sha256", "payload_sha256", "artifact_sha256")
            if name in calendar
        ),
        None,
    )
    if hash_field is None:
        return None
    provided_hash = _hash(calendar.get(hash_field), f"calendar.{hash_field}")
    body = {key: value for key, value in calendar.items() if key != hash_field}
    if provided_hash != _sha256(body):
        raise LearningQueueValidationError("calendar hash does not match canonical calendar")
    dates: list[str] = []
    previous: date | None = None
    for item in raw:
        normalized = _iso_date(item, "calendar.market_date")
        parsed = date.fromisoformat(normalized)
        if previous is not None and parsed <= previous:
            raise LearningQueueValidationError("calendar dates must be strictly increasing")
        if parsed.weekday() >= 5 or parsed in US_MARKET_HOLIDAYS:
            raise LearningQueueValidationError("calendar contains a non-session holiday/date")
        dates.append(normalized)
        previous = parsed
    identity = next(
        (
            calendar.get(name)
            for name in ("calendar_identity", "calendar_id", "identity")
            if calendar.get(name) is not None
        ),
        None,
    )
    if identity is not None and not isinstance(identity, (str, Mapping)):
        raise LearningQueueValidationError("calendar identity must be text or an object")
    identity_hash = (
        identity
        if isinstance(identity, str)
        else _sha256(identity)
        if identity is not None
        else None
    )
    return tuple(dates), provided_hash, identity_hash


def _next_validation_date(
    last_seen: str, policy: LearningQueuePolicy, calendar: Any
) -> tuple[str | None, str]:
    details = _calendar_details(calendar)
    if details is None:
        return None, "NOT_EVALUABLE_CALENDAR_REQUIRED"
    dates, _calendar_hash, _identity = details
    anchor = date.fromisoformat(last_seen)
    future = [item for item in dates if date.fromisoformat(item) > anchor]
    if len(future) < policy.validation_lag_sessions:
        return None, "NOT_EVALUABLE_CALENDAR_REQUIRED"
    return future[policy.validation_lag_sessions - 1], "EVALUABLE"


def _count(proposal: Mapping[str, Any], names: tuple[str, ...]) -> int | None:
    for name in names:
        if name in proposal:
            return _nonnegative_int(proposal[name], f"proposal.{name}")
    return None


def _evidence_descriptor(proposal: Mapping[str, Any]) -> tuple[str | None, bool]:
    """Extract an exact evidence identity and an explicit disjointness proof."""

    value = next(
        (proposal.get(name) for name in _EVIDENCE_ID_FIELDS if proposal.get(name) is not None),
        None,
    )
    if value is None:
        return None, False
    identity = _sha256(value)
    contract = proposal.get("evidence_disjointness_contract")
    proven = proposal.get("evidence_disjointness_proven") is True
    proven = proven or proposal.get("evidence_is_disjoint") is True
    proven = proven or proposal.get("evidence_disjoint") is True
    proven = proven or proposal.get("disjoint_evidence") is True
    if isinstance(proposal.get("evidence_disjointness"), str):
        proven = proven or proposal["evidence_disjointness"].upper() in {
            "PROVEN",
            "DISJOINT",
            "EXACT",
        }
    if isinstance(contract, Mapping):
        proven = proven or contract.get("proven") is True or contract.get("status") in {
            "PROVEN",
            "DISJOINT",
            "EXACT",
        }
    elif isinstance(contract, str):
        proven = proven or contract.upper() in {"PROVEN", "DISJOINT", "EXACT"}
    return identity, proven


def _policy_family(proposal: Mapping[str, Any], policy: LearningQueuePolicy) -> Any:
    value = proposal.get("policy_family")
    if value is None:
        value = proposal.get("policy_id")
    if value is None:
        return {"policy_id": policy.policy_id, "schema_version": POLICY_SCHEMA}
    if isinstance(value, str) and value.strip():
        return {"policy_id": value.strip(), "schema_version": POLICY_SCHEMA}
    if isinstance(value, Mapping) and value:
        return dict(value)
    raise LearningQueueValidationError("proposal policy family identity is invalid")


def _safe_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Retain proposal evidence while excluding untrusted return claims."""

    def scrub(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: scrub(child)
                for key, child in value.items()
                if not _RETURN_CLAIM_RE.search(str(key))
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [scrub(child) for child in value]
        return value

    return scrub(proposal)


def _evi_score(
    sample: int | None,
    sessions: int | None,
    occurrences: int,
    policy: LearningQueuePolicy,
) -> int:
    if sample is None or sessions is None:
        return 0
    sample_fraction = (
        Fraction(min(sample, policy.minimum_sample_count), policy.minimum_sample_count)
        if policy.minimum_sample_count
        else Fraction(1)
    )
    session_fraction = (
        Fraction(min(sessions, policy.minimum_session_count), policy.minimum_session_count)
        if policy.minimum_session_count
        else Fraction(1)
    )
    recurrence_fraction = Fraction(min(occurrences, 3), 3)
    score = (
        sample_fraction * policy.sample_weight_bps
        + session_fraction * policy.session_weight_bps
        + recurrence_fraction * policy.recurrence_weight_bps
    )
    return score.numerator // score.denominator


def _artifact_pair(item: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(item, Mapping) and "receipt" in item:
        proposal_value = item.get("proposals", item.get("proposal_artifact"))
        if proposal_value is not None:
            return _read_json(item["receipt"], "receipt"), _read_json(
                proposal_value, "proposals"
            )
    if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
        return _read_json(item[0], "receipt"), _read_json(item[1], "proposals")
    if isinstance(item, (str, Path)):
        root = Path(item)
        return (
            _read_json(root / "daily_learning_receipt.json", "receipt"),
            _read_json(root / "remediation_proposals.json", "proposals"),
        )
    raise LearningQueueValidationError("each daily artifact must be a receipt/proposals pair")


def _validate_pair(
    item: Any, as_of: str | None, policy: LearningQueuePolicy
) -> list[dict[str, Any]]:
    receipt, proposals = _artifact_pair(item)
    receipt_hash = _validate_artifact_hash(receipt, "receipt_sha256")
    proposal_hash = _validate_artifact_hash(proposals, "artifact_sha256")
    if receipt.get("schema_version") != "dawnstrike.strategy_learning_daily.v1":
        raise LearningQueueValidationError("unsupported receipt schema")
    _safety(receipt, label="receipt")
    _safety(proposals, label="proposals")
    if proposals.get("schema_version") != "dawnstrike.strategy_remediation_proposals.v1":
        raise LearningQueueValidationError("unsupported proposal schema")
    market_date = _iso_date(receipt.get("market_date"), "receipt.market_date")
    if proposals.get("market_date") != market_date:
        raise LearningQueueValidationError("receipt and proposal market_date differ")
    if as_of is not None and date.fromisoformat(market_date) > date.fromisoformat(as_of):
        raise LearningQueueValidationError("future market date is not consumable")
    for field in ("run_id", "input_hash_sha256", "cutoff"):
        if receipt.get(field) != proposals.get(field):
            raise LearningQueueValidationError(f"receipt and proposal {field} differ")
    receipt_lineage = _lineage(receipt, label="receipt")
    # Older daily proposal artifacts intentionally contain only the input
    # binding; the receipt is the authoritative source/code boundary.  When a
    # newer proposal artifact repeats lineage fields, every repeated field is
    # checked rather than trusted.  The two derived fields make the frozen
    # config/window identity explicit without inventing a mutable setting.
    lineage_names = {
        "source_hash_sha256": ("source_hash_sha256", "source_hash"),
        "input_hash_sha256": ("input_hash_sha256", "input_hash"),
        "config_hash_sha256": ("config_hash_sha256", "configuration_hash_sha256"),
        "code_sha": ("code_sha", "code_hash_sha256", "code_sha256"),
        "window_hash_sha256": ("window_hash_sha256", "window_hash"),
    }
    for canonical, names in lineage_names.items():
        if not any(name in proposals for name in names):
            continue
        proposal_value = next(proposals[name] for name in names if name in proposals)
        if canonical == "code_sha":
            if (
                not isinstance(proposal_value, str)
                or proposal_value.strip() != receipt_lineage[canonical]
            ):
                raise LearningQueueValidationError("receipt and proposal lineage differs")
        elif _hash(proposal_value, f"proposals.{canonical}") != receipt_lineage[canonical]:
            raise LearningQueueValidationError("receipt and proposal lineage differs")
    raw = proposals.get("proposals")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise LearningQueueValidationError("proposals.proposals must be a list")
    rows: list[dict[str, Any]] = []
    for index, proposal in enumerate(raw):
        if not isinstance(proposal, Mapping):
            raise LearningQueueValidationError(f"proposal[{index}] must be an object")
        row = dict(proposal)
        if row.get("status") != "PROPOSED_NOT_APPLIED" or row.get("applied") is not False:
            raise LearningQueueValidationError("only PROPOSED_NOT_APPLIED proposals are consumable")
        if row.get("market_date") is not None and row.get("market_date") != market_date:
            raise LearningQueueValidationError("proposal market_date differs from receipt")
        _safety(row, label=f"proposal[{index}]")
        strategy_id = row.get("strategy_id")
        strategy_version = row.get("strategy_version")
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise LearningQueueValidationError("proposal strategy_id is required")
        if not isinstance(strategy_version, str) or not strategy_version.strip():
            raise LearningQueueValidationError("proposal strategy_version is required")
        reason = _proposal_reason(row)
        variable = _proposal_variable(row)
        evidence_identity, disjointness_proven = _evidence_descriptor(row)
        for key in _COUNT_FIELDS:
            if key in row:
                _nonnegative_int(row[key], f"proposal.{key}")
        row["_queue"] = {
            "receipt_hash_sha256": receipt_hash,
            "proposal_artifact_hash_sha256": proposal_hash,
            "market_date": market_date,
            "lineage": receipt_lineage,
            "reason_identity": reason,
            "proposed_variable_identity": variable,
            "policy_family": _policy_family(row, policy),
            "evidence_identity": evidence_identity,
            "disjointness_proven": disjointness_proven,
            "proposal_identity": _sha256(
                {
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "reason": reason,
                    "proposed_variable": variable,
                }
            ),
        }
        rows.append(row)
    return rows


def build_managed_learning_queue(
    artifacts: Sequence[Any] | None = None,
    *,
    receipts: Sequence[Any] | None = None,
    proposal_artifacts: Sequence[Any] | None = None,
    as_of_market_date: str | None = None,
    policy: LearningQueuePolicy = DEFAULT_POLICY,
    calendar: Sequence[str] | Mapping[str, Any] | None = None,
    operator_assignment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume validated daily artifacts into one deterministic private queue."""

    if artifacts is None and receipts is not None and proposal_artifacts is not None:
        if len(receipts) != len(proposal_artifacts):
            raise LearningQueueValidationError("receipts and proposal_artifacts lengths differ")
        artifacts = tuple(
            {"receipt": receipt, "proposals": proposal}
            for receipt, proposal in zip(receipts, proposal_artifacts, strict=True)
        )
    if artifacts is None:
        raise LearningQueueValidationError("artifacts are required")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        raise LearningQueueValidationError("artifacts must be a sequence")
    as_of = _iso_date(as_of_market_date, "as_of_market_date") if as_of_market_date else None
    if operator_assignment is not None:
        operator_id = operator_assignment.get("operator_id")
        if (
            operator_assignment.get("authenticated") is not True
            or not isinstance(operator_id, str)
            or not operator_id.strip()
        ):
            raise LearningQueueValidationError(
                "operator assignment requires authenticated operator_id"
            )
    rows: list[dict[str, Any]] = []
    seen_occurrences: dict[tuple[str, str], str] = {}
    for item in artifacts:
        for row in _validate_pair(item, as_of, policy):
            identity = row["_queue"]
            occurrence_key = (identity["receipt_hash_sha256"], identity["proposal_identity"])
            content_hash = _sha256({key: value for key, value in row.items() if key != "_queue"})
            previous = seen_occurrences.get(occurrence_key)
            if previous is not None:
                if previous != content_hash:
                    raise LearningQueueValidationError("conflicting duplicate proposal occurrence")
                continue
            seen_occurrences[occurrence_key] = content_hash
            rows.append(row)

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = row.pop("_queue")
        group_key = _sha256(
            {
                "strategy_id": row["strategy_id"],
                "strategy_version": row["strategy_version"],
                "reason": identity["reason_identity"],
                "proposed_variable": identity["proposed_variable_identity"],
                "policy_family": identity["policy_family"],
            }
        )
        group = groups.setdefault(
            group_key,
            {
                "queue_item_id": "mlq-" + group_key[:24],
                "identity": {
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "reason": identity["reason_identity"],
                    "proposed_variable": identity["proposed_variable_identity"],
                    "policy_family": identity["policy_family"],
                },
                "occurrences": [],
                "evidence": {},
                "evidence_contract_proven": True,
                "sample_count": 0,
                "session_count": 0,
                "sample_count_known": True,
                "session_count_known": True,
                "evidence_count_conflict": False,
            },
        )
        sample = _count(row, ("sample_count", "eligible_sample_count", "evidence_sample_count"))
        sessions = _count(
            row, ("session_count", "eligible_session_count", "evidence_session_count")
        )
        evidence_identity = identity["evidence_identity"]
        disjointness_proven = identity["disjointness_proven"]
        group["evidence_contract_proven"] &= disjointness_proven and evidence_identity is not None
        if evidence_identity is None:
            if not group["occurrences"]:
                if sample is not None:
                    group["sample_count"] = sample
                else:
                    group["sample_count_known"] = False
                if sessions is not None:
                    group["session_count"] = sessions
                else:
                    group["session_count_known"] = False
            else:
                group["sample_count_known"] = False
                group["session_count_known"] = False
        else:
            previous_evidence = group["evidence"].get(evidence_identity)
            current_counts = (sample, sessions)
            if previous_evidence is None:
                group["evidence"][evidence_identity] = current_counts
                if sample is not None:
                    group["sample_count"] += sample
                else:
                    group["sample_count_known"] = False
                if sessions is not None:
                    group["session_count"] += sessions
                else:
                    group["session_count_known"] = False
            elif previous_evidence != current_counts:
                group["evidence_count_conflict"] = True
        group["occurrences"].append(
            {
                "market_date": identity["market_date"],
                "receipt_hash_sha256": identity["receipt_hash_sha256"],
                "proposal_artifact_hash_sha256": identity["proposal_artifact_hash_sha256"],
                "input_hash_sha256": identity["lineage"]["input_hash_sha256"],
                "lineage": identity["lineage"],
                "run_id": row.get("run_id"),
                "cutoff": row.get("cutoff"),
                "evidence_identity": evidence_identity,
                "proposal": _safe_proposal(row),
            }
        )

    items: list[dict[str, Any]] = []
    for group in groups.values():
        occurrences = sorted(
            group.pop("occurrences"),
            key=lambda item: (
                item["market_date"],
                item["receipt_hash_sha256"],
                item["proposal_artifact_hash_sha256"],
            ),
        )
        sample_known = group.pop("sample_count_known")
        session_known = group.pop("session_count_known")
        contract_proven = group.pop("evidence_contract_proven")
        count_conflict = group.pop("evidence_count_conflict")
        group.pop("evidence")
        single_occurrence_without_aggregation = len(occurrences) == 1 and not contract_proven
        count_contract_ok = contract_proven or single_occurrence_without_aggregation
        sample = (
            group.pop("sample_count")
            if sample_known and count_contract_ok and not count_conflict
            else None
        )
        sessions = (
            group.pop("session_count")
            if session_known and count_contract_ok and not count_conflict
            else None
        )
        if not contract_proven and not single_occurrence_without_aggregation:
            evidence_reason = (
                "sample/session aggregation withheld: each evidence identity requires "
                "an explicit proven disjointness contract"
            )
        elif count_conflict:
            evidence_reason = (
                "sample/session aggregation withheld: duplicate evidence identity has "
                "conflicting counts"
            )
        elif sample is None or sessions is None:
            evidence_reason = "sample/session evidence is incomplete"
        else:
            evidence_reason = None
        maturity = (
            "MATURE"
            if sample is not None
            and sessions is not None
            and sample >= policy.minimum_sample_count
            and sessions >= policy.minimum_session_count
            else "COLLECT_EVIDENCE"
        )
        score = _evi_score(sample, sessions, len(occurrences), policy)
        last_seen = occurrences[-1]["market_date"]
        owner = "REVIEW_REQUIRED" if maturity == "MATURE" else "UNASSIGNED"
        next_date, calendar_status = _next_validation_date(last_seen, policy, calendar)
        item = {
            **group,
            "occurrence_count": len(occurrences),
            "occurrences": occurrences,
            "first_seen_market_date": occurrences[0]["market_date"],
            "last_seen_market_date": last_seen,
            "receipt_hashes_sha256": sorted({item["receipt_hash_sha256"] for item in occurrences}),
            "sample_count": sample,
            "session_count": sessions,
            "evidence_maturity": maturity,
            "evidence_maturity_reason": evidence_reason,
            "minimums": {
                "sample_count": policy.minimum_sample_count,
                "session_count": policy.minimum_session_count,
            },
            "evi_score_bps": score,
            "ownership_status": owner,
            "next_eligible_validation_date": next_date,
            "status": calendar_status,
        }
        if operator_assignment is not None:
            item["ownership_status"] = "ASSIGNED"
            item["operator_assignment"] = {
                "operator_id": operator_assignment["operator_id"],
                "authenticated": True,
            }
        items.append(item)

    items.sort(key=lambda item: (-item["evi_score_bps"], item["queue_item_id"]))
    calendar_details = _calendar_details(calendar)
    calendar_hash = calendar_details[1] if calendar_details is not None else None
    calendar_identity = calendar_details[2] if calendar_details is not None else None
    body = {
        "schema_version": QUEUE_SCHEMA,
        "policy": policy.to_dict(),
        "calendar_hash_sha256": calendar_hash,
        "calendar_identity": calendar_identity,
        "as_of_market_date": as_of,
        "items": items,
        "research_only": True,
        "broker_execution_enabled": False,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "return_claims": None,
        "atomic_daily_learning_integration": "UNSAFE_EXISTING_MANIFEST_CONTRACT_UNCHANGED",
    }
    body["queue_sha256"] = _sha256(body)
    return body


def write_managed_learning_queue(path: str | Path, queue: Mapping[str, Any]) -> bool:
    """Install an immutable queue file with first-writer-wins semantics."""

    payload = dict(queue)
    expected = _hash(payload.get("queue_sha256"), "queue_sha256")
    if expected != _sha256({key: value for key, value in payload.items() if key != "queue_sha256"}):
        raise LearningQueueValidationError("queue_sha256 does not match canonical queue")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise LearningQueueValidationError("queue path must not be a symlink")
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if destination.is_symlink() or destination.read_bytes() != encoded:
            raise LearningQueueValidationError("immutable queue conflict") from None
        return True
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return False


def _approved_root(path: str | Path) -> Path:
    root = Path(path)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise LearningQueueValidationError("approved root must be an existing absolute directory")
    return root.resolve(strict=True)


def _reject_symlink_ancestors(path: Path) -> None:
    for ancestor in path.parents:
        if ancestor.is_symlink():
            raise LearningQueueValidationError("queue path contains a symlink escape")


def discover_committed_learning_pairs(approved_root: str | Path) -> tuple[Path, ...]:
    """Discover only committed, non-symlink daily artifact directories."""

    root = _approved_root(approved_root)
    discovered: list[Path] = []
    for current, directories, _files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in directories:
            candidate = current_path / name
            if candidate.is_symlink():
                raise LearningQueueValidationError("approved root contains a symlink escape")
            safe_directories.append(name)
        directories[:] = safe_directories
        receipt = current_path / "daily_learning_receipt.json"
        proposals = current_path / "remediation_proposals.json"
        manifest = current_path / "daily_learning_commit_manifest.json"
        if not (receipt.exists() or proposals.exists() or manifest.exists()):
            continue
        if receipt.is_symlink() or proposals.is_symlink() or manifest.is_symlink():
            raise LearningQueueValidationError("daily learning artifact path must not be a symlink")
        if not (receipt.is_file() and proposals.is_file() and manifest.is_file()):
            continue
        try:
            from intraday_scanner.services.daily_strategy_learning_service import (
                _validate_commit_manifest,
            )

            committed = _validate_commit_manifest(current_path)
            if committed is None:
                continue
            pair = (receipt, proposals)
            _validate_pair(pair, None, DEFAULT_POLICY)
        except (LearningQueueValidationError, OSError, ValueError, TypeError):
            continue
        discovered.append(current_path)
    return tuple(sorted(discovered, key=lambda path: str(path)))


def _install_content_addressed(path: Path, payload: bytes) -> bool:
    _reject_symlink_ancestors(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LearningQueueValidationError("content-addressed queue path must not be a symlink")
    if path.is_file():
        if path.read_bytes() != payload:
            raise LearningQueueValidationError("content-addressed queue conflict")
        return True
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".queue-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != payload:
                raise LearningQueueValidationError("content-addressed queue conflict") from None
            return True
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _update_latest_pointer(path: Path, payload: Mapping[str, Any]) -> bool:
    """Atomically install a monotonic latest pointer under an exclusive lock."""

    _reject_symlink_ancestors(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise LearningQueueValidationError("latest pointer update is busy; retry") from None
    try:
        encoded = (_canonical_json(dict(payload)) + "\n").encode("utf-8")
        if path.is_symlink():
            raise LearningQueueValidationError("latest pointer must not be a symlink")
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LearningQueueValidationError("latest pointer is malformed") from exc
            if not isinstance(existing, Mapping):
                raise LearningQueueValidationError("latest pointer is malformed")
            existing_body = {
                key: value for key, value in existing.items() if key != "pointer_sha256"
            }
            if existing.get("pointer_sha256") != _sha256(existing_body):
                raise LearningQueueValidationError("latest pointer hash mismatch")
            if existing == dict(payload):
                return True
            old_date = str(existing.get("last_seen_market_date") or "")
            new_date = str(payload.get("last_seen_market_date") or "")
            if old_date and new_date <= old_date:
                raise LearningQueueValidationError("latest pointer conflict")
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".latest-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return False
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)


def produce_managed_learning_queue(
    approved_root: str | Path,
    output_root: str | Path,
    *,
    calendar: Sequence[str] | Mapping[str, Any] | None = None,
    as_of_market_date: str | None = None,
    policy: LearningQueuePolicy = DEFAULT_POLICY,
) -> dict[str, Any]:
    """Post-commit producer for the private queue; daily artifacts stay untouched."""

    if calendar is None:
        return {"status": "NOT_EVALUABLE_CALENDAR_REQUIRED", "pair_count": 0}
    pairs = discover_committed_learning_pairs(approved_root)
    if not pairs:
        return {"status": "NO_ELIGIBLE_COMMITTED_ARTIFACTS", "pair_count": 0}
    queue = build_managed_learning_queue(
        pairs,
        as_of_market_date=as_of_market_date,
        policy=policy,
        calendar=calendar,
    )
    output = Path(output_root)
    if not output.is_absolute() or output.is_symlink():
        raise LearningQueueValidationError("output root must be an absolute non-symlink directory")
    _reject_symlink_ancestors(output)
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve(strict=True)
    queue_hash = str(queue["queue_sha256"])
    encoded = (_canonical_json(queue) + "\n").encode("utf-8")
    _install_content_addressed(output / "content" / f"{queue_hash}.json", encoded)
    dates = sorted(
        {
            occurrence["market_date"]
            for item in queue["items"]
            for occurrence in item["occurrences"]
        }
    )
    for market_date in dates:
        _install_content_addressed(output / "history" / market_date / f"{queue_hash}.json", encoded)
    pointer = {
        "schema_version": "dawnstrike.managed_strategy_learning_queue.latest.v1",
        "queue_sha256": queue_hash,
        "last_seen_market_date": max(dates) if dates else None,
        "queue_path": f"content/{queue_hash}.json",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    pointer["pointer_sha256"] = _sha256(pointer)
    _update_latest_pointer(output / "latest.json", pointer)
    return {"status": "COMPLETE", "pair_count": len(pairs), "queue": queue, "latest": pointer}


def consume_daily_learning_proposals(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility entry point with an explicit, private name."""

    return build_managed_learning_queue(*args, **kwargs)


run_managed_learning_queue = build_managed_learning_queue
coalesce_daily_remediation_proposals = build_managed_learning_queue
persist_managed_learning_queue = write_managed_learning_queue


class ManagedLearningQueueService:
    """Small façade for callers that prefer an object-bound frozen policy."""

    def __init__(self, policy: LearningQueuePolicy = DEFAULT_POLICY) -> None:
        self.policy = policy

    def build(self, artifacts: Sequence[Any], **kwargs: Any) -> dict[str, Any]:
        return build_managed_learning_queue(artifacts, policy=self.policy, **kwargs)

    def write(self, path: str | Path, queue: Mapping[str, Any]) -> bool:
        return write_managed_learning_queue(path, queue)


__all__ = [
    "DEFAULT_POLICY",
    "LearningQueuePolicy",
    "LearningQueueValidationError",
    "ManagedLearningQueueService",
    "build_managed_learning_queue",
    "coalesce_daily_remediation_proposals",
    "consume_daily_learning_proposals",
    "discover_committed_learning_pairs",
    "persist_managed_learning_queue",
    "produce_managed_learning_queue",
    "run_managed_learning_queue",
    "write_managed_learning_queue",
]
