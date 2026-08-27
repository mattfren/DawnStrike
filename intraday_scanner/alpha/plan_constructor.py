"""Frozen, source-bound AlphaOps v5 market-structure plans.

The constructor is deliberately separate from reward/risk evaluation.  It
freezes levels from completed observations first; the cost and risk gates are
then free to reject the frozen plan.  In particular, a target is never moved
to make a reward/risk threshold pass.  Observation ``source_url`` values must
be public HTTP(S) references or a narrowly scoped ``internal://`` source ID;
the latter still requires a nonblank source identity, completed timestamps,
and a SHA-256 source hash.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import ipaddress
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from intraday_scanner.decisioning.contracts import canonical_json

NO_VALID_PLAN = "NO_VALID_PLAN"
COMPLETE = "COMPLETE"
LEGACY_RESEARCH_BASELINE = "LEGACY_RESEARCH_BASELINE"
PLAN_SCHEMA_VERSION = "dawnstrike.alphaops_market_structure_plan.v1"
SUPPORTED_TARGET_BASES = frozenset(
    {
        "sourced_resistance",
        "prior_resistance",
        "prior_swing_high",
        "prior_day_resistance",
        "prior_week_resistance",
        "gap_boundary",
        "liquidity_level",
        "vwap_structure",
    }
)
SUPPORTED_OBSERVATION_KINDS = frozenset(
    {
        "sourced_entry",
        "sourced_stop",
        "sourced_invalidation",
        "premarket_price",
        "premarket_low",
        "prior_swing_high",
        "prior_day_resistance",
        "prior_week_resistance",
        "sourced_resistance",
        "prior_resistance",
        "gap_boundary",
        "liquidity_level",
        "vwap_structure",
    }
)
ALLOWED_DERIVATION_POLICIES = frozenset({"identity", "direct_observation"})
_SHA256 = frozenset("0123456789abcdef")
_INTERNAL_SOURCE_URL = re.compile(r"^internal://[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")
_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "direction",
        "entry",
        "stop",
        "target",
        "target_basis_kind",
        "target_frozen_at",
        "observations",
        "plan_hash_sha256",
        "reason",
        "research_only",
        "broker_execution_enabled",
        "target_frozen_before_reward_risk",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "role",
        "value",
        "observed_at",
        "completed_at",
        "source",
        "source_hash",
        "observation_kind",
        "raw_value",
        "derivation_policy",
        "source_url",
        "observation_hash",
        "is_complete",
    }
)


@dataclass(frozen=True, slots=True)
class PlanObservation:
    """One independently attributable completed market observation."""

    role: str
    value: float
    observed_at: str
    completed_at: str
    source: str
    source_hash: str
    observation_kind: str
    raw_value: float
    derivation_policy: str
    source_url: str = ""
    observation_hash: str = ""
    is_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AlphaOpsMarketStructurePlan:
    """A content-addressed plan whose levels cannot be changed in-place."""

    status: str
    direction: str
    entry: float | None
    stop: float | None
    target: float | None
    target_basis_kind: str
    target_frozen_at: str
    observations: tuple[PlanObservation, ...]
    plan_hash_sha256: str
    reason: str = ""
    research_only: bool = True
    broker_execution_enabled: bool = False

    @property
    def is_complete(self) -> bool:
        return self.status == COMPLETE

    # Price-oriented aliases make the frozen contract easy to consume from
    # receipt and paper-reconciliation code without duplicating values.
    @property
    def entry_price(self) -> float | None:
        return self.entry

    @property
    def stop_price(self) -> float | None:
        return self.stop

    @property
    def target_price(self) -> float | None:
        return self.target

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["observations"] = [item.to_dict() for item in self.observations]
        payload["schema_version"] = PLAN_SCHEMA_VERSION
        payload["target_frozen_before_reward_risk"] = self.is_complete
        return payload

    def compute_hash(self) -> str:
        """Recompute the hash over the exact emitted payload, minus its hash."""

        payload = self.to_dict()
        payload.pop("plan_hash_sha256", None)
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())


def construct_alphaops_v5_plan(
    signal: Mapping[str, Any] | None = None,
    *,
    decision_at: str | None = None,
    direction: str | None = None,
    observations: Mapping[str, Any] | None = None,
    entry_observation: Mapping[str, Any] | None = None,
    stop_observation: Mapping[str, Any] | None = None,
    target_observation: Mapping[str, Any] | None = None,
    target_observations: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
) -> AlphaOpsMarketStructurePlan:
    """Construct and freeze a v5 structural plan, or return ``NO_VALID_PLAN``.

    ``signal`` may carry explicit ``market_structure_observations`` (the
    preferred interface) or the equivalent ``plan_observations`` mapping.  A
    flat signal is accepted only when it has complete provenance aliases for
    every leg; values alone are intentionally insufficient.
    """

    signal = dict(signal or {})
    if observations is not None:
        signal["market_structure_observations"] = observations
    if entry_observation is not None:
        signal["entry_observation"] = entry_observation
    if stop_observation is not None:
        signal["stop_observation"] = stop_observation
    if target_observation is not None:
        signal["target_observation"] = target_observation
    if target_observations is not None:
        signal["target_observations"] = target_observations
    normalized_direction = str(signal.get("direction") or direction or "long").lower()
    if normalized_direction not in {"long", "short"}:
        return _invalid(normalized_direction, "direction_unknown")
    if decision_at is not None and _parse_timestamp(decision_at) is None:
        return _invalid(normalized_direction, "decision_time_invalid")
    declared = signal.get("market_structure_observations") or signal.get("plan_observations")
    observations: dict[str, Any] = dict(declared) if isinstance(declared, Mapping) else {}
    target_candidates = signal.get("target_observations") or signal.get("target_candidates")
    if isinstance(target_candidates, (list, tuple)):
        observations.setdefault("target_candidates", target_candidates)

    declared_entry = observations.get("entry")
    declared_stop = observations.get("stop")
    entry_observation = (
        declared_entry
        if isinstance(declared_entry, Mapping)
        else signal.get("entry_observation")
    )
    stop_observation = (
        declared_stop
        if isinstance(declared_stop, Mapping)
        else signal.get("stop_observation")
    )
    entry_value = _first(
        signal,
        "entry",
        "entry_reference",
        "entry_watch_level",
        "entry_trigger",
        "breakout_trigger",
    )
    if entry_value is None and isinstance(entry_observation, Mapping):
        entry_value = _first(entry_observation, "value", "price", "level")
    stop_value = _first(
        signal,
        "stop",
        "stop_price",
        "invalidation_level",
        "invalidation",
        "exit_line",
    )
    if stop_value is None and isinstance(stop_observation, Mapping):
        stop_value = _first(stop_observation, "value", "price", "level")
    values = {
        "entry": _number(entry_value),
        "stop": _number(stop_value),
    }
    target_value = _number(_first(signal, "target", "target_1", "first_target"))
    target_basis = str(_first(signal, "target_basis_kind", "target_basis", "target_role") or "").strip().lower()
    target_derived = _bool(_first(signal, "target_derived_from_risk"))
    if target_derived is True or target_basis in {"risk_multiple", "fixed_rr", "risk_derived"}:
        return _invalid(normalized_direction, "target_is_risk_derived")

    # Build leg provenance.  A target candidate list is frozen in listed order
    # and never searched again after reward/risk is evaluated.
    legs: dict[str, PlanObservation] = {}
    for role in ("entry", "stop"):
        leg = _observation_for(
            role, values[role], observations.get(role), signal, decision_at=decision_at
        )
        if leg is None:
            return _invalid(normalized_direction, f"{role}_observation_incomplete")
        if leg.value != values[role]:
            return _invalid(normalized_direction, f"{role}_observation_level_mismatch")
        legs[role] = leg

    candidates = _target_candidates(signal, observations, target_value, target_basis)
    selected: PlanObservation | None = None
    selected_basis = ""
    for _index, candidate in enumerate(candidates):
        value = _number(_first(candidate, "value", "target", "target_1", "first_target"))
        basis = str(
            _first(
                candidate,
                "target_basis_kind",
                "target_basis",
                "basis",
                "observation_kind",
                "role",
            )
            or target_basis
        ).strip().lower()
        candidate_derived = _bool(
            _first(candidate, "target_derived_from_risk", "derived_from_risk")
        )
        if candidate_derived is True or basis in {"risk_multiple", "fixed_rr", "risk_derived"}:
            continue
        if value is None:
            continue
        if not basis or basis not in SUPPORTED_TARGET_BASES:
            continue
        leg = _observation_for(
            "target", value, candidate, signal, decision_at=decision_at
        )
        if leg is None:
            continue
        # Structural validity determines candidate eligibility.  RR is
        # evaluated later and cannot influence this selection.
        if _valid_geometry(values["entry"], values["stop"], value, normalized_direction):
            selected = leg
            # The emitted basis is the observed structural kind, never a
            # caller-supplied alias that could obscure what was observed.
            selected_basis = leg.observation_kind
            break
    if selected is None:
        return _invalid(normalized_direction, "target_observation_or_geometry_invalid")
    legs["target"] = selected

    entry = values["entry"]
    stop = values["stop"]
    target = selected.value
    if not _valid_geometry(entry, stop, target, normalized_direction):
        return _invalid(normalized_direction, "plan_geometry_invalid")
    frozen_at = str(
        decision_at
        or max(
            (leg.completed_at for leg in legs.values()),
            key=lambda value: _parse_timestamp(value) or datetime.min.replace(tzinfo=timezone.utc),
        )
    )
    plan = AlphaOpsMarketStructurePlan(
        status=COMPLETE,
        direction=normalized_direction,
        entry=entry,
        stop=stop,
        target=target,
        target_basis_kind=selected_basis,
        target_frozen_at=frozen_at,
        observations=tuple(legs[name] for name in ("entry", "stop", "target")),
        plan_hash_sha256="",
    )
    return _with_assigned_hash(plan)


def build_alphaops_v5_plan(*args: Any, **kwargs: Any) -> AlphaOpsMarketStructurePlan:
    """Compatibility alias used by cycle callers and external audits."""

    return construct_alphaops_v5_plan(*args, **kwargs)


def build_market_structure_plan(*args: Any, **kwargs: Any) -> AlphaOpsMarketStructurePlan:
    """Generic alias for integrations that do not name the v5 policy."""

    return construct_alphaops_v5_plan(*args, **kwargs)


def _invalid(direction: str, reason: str) -> AlphaOpsMarketStructurePlan:
    plan = AlphaOpsMarketStructurePlan(
        status=NO_VALID_PLAN,
        direction=direction,
        entry=None,
        stop=None,
        target=None,
        target_basis_kind="",
        target_frozen_at="",
        observations=(),
        plan_hash_sha256="",
        reason=reason,
    )
    return _with_assigned_hash(plan)


def _with_assigned_hash(plan: AlphaOpsMarketStructurePlan) -> AlphaOpsMarketStructurePlan:
    """Assign the hash only after the full serialized plan shape exists."""

    return replace(plan, plan_hash_sha256=plan.compute_hash())


def validate_alphaops_v5_plan(
    contract: AlphaOpsMarketStructurePlan | Mapping[str, Any],
    *,
    raise_on_error: bool = True,
) -> bool:
    """Strictly validate an emitted plan contract and its content hashes.

    The validator accepts the serialized mapping produced by ``to_dict`` (or
    a plan object for convenience). It recomputes the plan hash over every
    emitted field except ``plan_hash_sha256`` and independently recomputes
    each observation hash. Set ``raise_on_error=False`` when a boolean probe
    is preferred; strict callers get a ``ValueError`` for every violation.
    """

    try:
        _validate_alphaops_v5_plan(contract)
    except (TypeError, ValueError, KeyError) as exc:
        if raise_on_error:
            raise ValueError(f"invalid AlphaOps v5 plan: {exc}") from exc
        return False
    return True


def is_valid_alphaops_v5_plan(
    contract: AlphaOpsMarketStructurePlan | Mapping[str, Any],
) -> bool:
    """Boolean companion to :func:`validate_alphaops_v5_plan`."""

    return validate_alphaops_v5_plan(contract, raise_on_error=False)


def _validate_alphaops_v5_plan(
    contract: AlphaOpsMarketStructurePlan | Mapping[str, Any],
) -> None:
    payload = (
        contract.to_dict()
        if isinstance(contract, AlphaOpsMarketStructurePlan)
        else dict(contract)
    )
    if set(payload) != _PLAN_FIELDS:
        raise ValueError("plan schema fields do not match v1 contract")
    for field in (
        "schema_version",
        "status",
        "direction",
        "target_basis_kind",
        "target_frozen_at",
        "reason",
        "plan_hash_sha256",
    ):
        if type(payload[field]) is not str or payload[field] != payload[field].strip():
            raise ValueError(f"{field} must be a string")
    for field in ("research_only", "broker_execution_enabled", "target_frozen_before_reward_risk"):
        if type(payload[field]) is not bool:
            raise ValueError(f"{field} must be a boolean")
    if payload["schema_version"] != PLAN_SCHEMA_VERSION:
        raise ValueError("unsupported plan schema version")
    supplied_hash = payload["plan_hash_sha256"]
    if not _is_sha256(supplied_hash):
        raise ValueError("plan hash is missing or invalid")
    hash_payload = dict(payload)
    hash_payload.pop("plan_hash_sha256", None)
    expected_hash = hashlib.sha256(
        canonical_json(hash_payload).encode("utf-8")
    ).hexdigest()
    if supplied_hash != expected_hash:
        raise ValueError("plan hash does not match emitted payload")
    if payload["research_only"] is not True or payload["broker_execution_enabled"] is not False:
        raise ValueError("plan must remain research-only with broker execution disabled")
    status = str(payload.get("status") or "")
    if not payload["direction"]:
        raise ValueError("plan direction is required")
    if payload["target_frozen_before_reward_risk"] is not (status == COMPLETE):
        raise ValueError("target freeze marker does not match plan status")
    if status == NO_VALID_PLAN:
        if (
            payload["entry"] is not None
            or payload["stop"] is not None
            or payload["target"] is not None
            or payload["target_basis_kind"] != ""
            or payload["target_frozen_at"] != ""
            or payload["observations"] != []
            or not str(payload.get("reason") or "").strip()
        ):
            raise ValueError("NO_VALID_PLAN contains qualifying levels or observations")
        return
    if status != COMPLETE:
        raise ValueError("unknown plan status")
    if payload["reason"] != "":
        raise ValueError("complete plan cannot carry a failure reason")
    direction = str(payload.get("direction") or "").lower()
    entry = _strict_number(payload.get("entry"))
    stop = _strict_number(payload.get("stop"))
    target = _strict_number(payload.get("target"))
    if direction not in {"long", "short"} or not _valid_geometry(entry, stop, target, direction):
        raise ValueError("complete plan geometry is invalid")
    if payload["target_basis_kind"] not in SUPPORTED_TARGET_BASES:
        raise ValueError("target basis is not an independent structural kind")
    freeze_at = _parse_timestamp(payload["target_frozen_at"])
    if freeze_at is None:
        raise ValueError("target freeze timestamp is invalid")
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, list) or [item.get("role") for item in raw_observations if isinstance(item, Mapping)] != ["entry", "stop", "target"]:
        raise ValueError("complete plan must contain entry, stop, and target observations")
    by_role: dict[str, Mapping[str, Any]] = {}
    for raw in raw_observations:
        if not isinstance(raw, Mapping):
            raise ValueError("observation must be an object")
        if set(raw) != _OBSERVATION_FIELDS:
            raise ValueError("observation schema fields do not match v1 contract")
        role = str(raw.get("role") or "")
        if role in by_role:
            raise ValueError("duplicate observation role")
        by_role[role] = raw
        _validate_observation(raw, role)
    if payload["target_basis_kind"] != by_role["target"]["observation_kind"]:
        raise ValueError("plan target basis does not match observed structural kind")
    if any(
        (_parse_timestamp(by_role[role]["completed_at"]) or freeze_at) > freeze_at
        for role in ("entry", "stop", "target")
    ):
        raise ValueError("plan freeze precedes a completed observation")
    for role, expected in (("entry", entry), ("stop", stop), ("target", target)):
        if _strict_number(by_role[role]["value"]) != expected:
            raise ValueError(f"{role} level does not match its observation")


def _validate_observation(raw: Mapping[str, Any], role: str) -> None:
    for field in (
        "role",
        "observed_at",
        "completed_at",
        "source",
        "source_hash",
        "observation_kind",
        "derivation_policy",
        "source_url",
        "observation_hash",
    ):
        if type(raw[field]) is not str or raw[field] != raw[field].strip():
            raise ValueError(f"{role} {field} must be a string")
    if type(raw["is_complete"]) is not bool:
        raise ValueError(f"{role} is_complete must be a boolean")
    value = _strict_number(raw.get("value"))
    raw_value = _strict_number(raw.get("raw_value"))
    if value is None or raw_value is None or value <= 0 or raw_value <= 0:
        raise ValueError(f"{role} observation values are invalid")
    if raw.get("is_complete") is not True:
        raise ValueError(f"{role} observation is not complete")
    kind = raw["observation_kind"]
    if kind not in SUPPORTED_OBSERVATION_KINDS:
        raise ValueError(f"{role} observation kind is unsupported")
    if role == "target":
        if kind not in SUPPORTED_TARGET_BASES or value != raw_value:
            raise ValueError("target must equal its raw observed structural level")
    derivation = raw["derivation_policy"]
    if derivation not in ALLOWED_DERIVATION_POLICIES:
        raise ValueError("observation derivation policy is not allowlisted")
    if not _derivation_reconciles_level(
        role=role,
        value=value,
        raw_value=raw_value,
        derivation_policy=derivation,
    ):
        raise ValueError(f"{role} level does not reconcile with its raw observation")
    if not str(raw.get("source") or "").strip() or not _source_reference_valid(raw.get("source_url")):
        raise ValueError(f"{role} observation source provenance is incomplete")
    source_hash = raw["source_hash"]
    if not _is_sha256(source_hash):
        raise ValueError(f"{role} observation source hash is invalid")
    observed_at = _parse_timestamp(raw.get("observed_at"))
    completed_at = _parse_timestamp(raw.get("completed_at"))
    if observed_at is None or completed_at is None or observed_at > completed_at:
        raise ValueError(f"{role} observation timestamps are invalid")
    observation_hash = raw["observation_hash"]
    hash_payload = dict(raw)
    hash_payload.pop("observation_hash", None)
    expected_hash = hashlib.sha256(
        canonical_json(hash_payload).encode("utf-8")
    ).hexdigest()
    if observation_hash != expected_hash:
        raise ValueError(f"{role} observation hash does not match emitted payload")


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and bool(text) and set(text) <= _SHA256


def _derivation_reconciles_level(
    *, role: str, value: float, raw_value: float, derivation_policy: str
) -> bool:
    """Recompute the emitted level from the declared derivation policy.

    ``identity`` and ``direct_observation`` both mean that the frozen plan
    level is exactly the completed source observation for every role. The
    explicit dispatch makes any future arithmetic policy a deliberate,
    separately validated schema change instead of an implicit trust boundary.
    """

    del role  # retained in the signature for useful call-site diagnostics
    if derivation_policy in {"identity", "direct_observation"}:
        return value == raw_value
    return False


def _strict_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def apply_structural_level_enrichment(
    signal: Mapping[str, Any],
    observations: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach upstream structural observations without deriving any levels.

    Providers should call this adapter after independently collecting entry,
    stop, and structural-target observations.  It intentionally performs no
    fallback, arithmetic, or target search; the constructor remains the sole
    validator and freezer.
    """

    if not isinstance(observations, Mapping):
        raise TypeError("structural observations must be a mapping")
    return {
        **dict(signal),
        "market_structure_observations": dict(observations),
        "legacy_plan_baseline": False,
    }


def with_structural_level_enrichment(
    signal: Mapping[str, Any], observations: Mapping[str, Any]
) -> dict[str, Any]:
    """Compatibility alias for upstream structural-level providers."""

    return apply_structural_level_enrichment(signal, observations)


def _target_candidates(
    signal: Mapping[str, Any], observations: Mapping[str, Any], value: float | None, basis: str
) -> list[Mapping[str, Any]]:
    raw = observations.get("target_candidates") or signal.get("target_observations") or signal.get("target_candidates")
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if isinstance(item, Mapping)]
    target = observations.get("target")
    if isinstance(target, Mapping):
        return [target]
    target = signal.get("target_observation")
    if isinstance(target, Mapping):
        return [target]
    if value is None:
        return []
    return [{"value": value, "target_basis_kind": basis, **_provenance_aliases(signal, "target")}]


def _observation_for(
    role: str,
    value: float | None,
    raw: Any,
    signal: Mapping[str, Any],
    *,
    decision_at: str | None = None,
) -> PlanObservation | None:
    data: dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {}
    if role == "target" and not data:
        data.update(_provenance_aliases(signal, role))
    aliases = _provenance_aliases(signal, role)
    for key, alias_value in aliases.items():
        data.setdefault(key, alias_value)
    numeric = _number(_first(data, "value", "price", "level"))
    if numeric is None:
        numeric = value
    observed_at = str(_first(data, "observed_at", "observation_time", "source_timestamp", "timestamp") or "").strip()
    completed_at = str(_first(data, "completed_at", "bar_completed_at", "completion_time") or "").strip()
    source = str(_first(data, "source", "source_identity", "provider") or "").strip()
    source_url = str(_first(data, "source_url", "url") or "").strip()
    source_hash = str(_first(data, "source_hash", "source_hash_sha256", "observation_sha256", "hash") or "").strip().lower()
    observation_kind = str(
        _first(data, "observation_kind", "basis_kind", "kind") or ""
    ).strip().lower()
    raw_value = _number(
        _first(data, "raw_value", "basis_value", "raw_basis_value")
    )
    derivation_policy = str(
        _first(data, "derivation_policy", "derivation", "level_policy") or ""
    ).strip().lower()
    complete = _bool(_first(data, "is_complete", "completed", "observation_complete"))
    if complete is not True or numeric is None or not math.isfinite(numeric) or numeric <= 0:
        return None
    if (
        not observed_at
        or not completed_at
        or not source
        or not observation_kind
        or raw_value is None
        or raw_value <= 0
        or not derivation_policy
        or len(source_hash) != 64
        or not set(source_hash) <= _SHA256
    ):
        return None
    if observation_kind not in SUPPORTED_OBSERVATION_KINDS:
        return None
    if role == "target" and observation_kind not in SUPPORTED_TARGET_BASES:
        return None
    if derivation_policy not in ALLOWED_DERIVATION_POLICIES:
        return None
    if role == "target" and numeric != raw_value:
        return None
    # v1 only admits observation-identity policies. Reconcile every leg,
    # including entry and stop, before freezing it into the plan; otherwise a
    # caller could smuggle a derived level through a freshly recomputed hash.
    if not _derivation_reconciles_level(
        role=role,
        value=numeric,
        raw_value=raw_value,
        derivation_policy=derivation_policy,
    ):
        return None
    if role == "target" and any(
        token in derivation_policy
        for token in ("range_extension", "risk_multiple", "atr_extension")
    ):
        return None
    observed_dt = _parse_timestamp(observed_at)
    completed_dt = _parse_timestamp(completed_at)
    decision_dt = _parse_timestamp(decision_at) if decision_at else None
    if observed_dt is None or completed_dt is None or observed_dt > completed_dt:
        return None
    if decision_dt is not None and completed_dt > decision_dt:
        return None
    if not _source_reference_valid(source_url):
        return None
    observation_payload = {
        "role": role,
        "value": numeric,
        "observed_at": observed_at,
        "completed_at": completed_at,
        "source": source,
        "source_url": source_url,
        "source_hash": source_hash,
        "observation_kind": observation_kind,
        "raw_value": raw_value,
        "derivation_policy": derivation_policy,
        "is_complete": True,
    }
    expected_hash = hashlib.sha256(canonical_json(observation_payload).encode("utf-8")).hexdigest()
    supplied_observation_hash = str(data.get("observation_hash") or "").strip().lower()
    if supplied_observation_hash and supplied_observation_hash != expected_hash:
        return None
    return PlanObservation(
        role=role,
        value=numeric,
        observed_at=observed_at,
        completed_at=completed_at,
        source=source,
        source_hash=source_hash,
        observation_kind=observation_kind,
        raw_value=raw_value,
        derivation_policy=derivation_policy,
        source_url=source_url,
        observation_hash=expected_hash,
    )


def _provenance_aliases(signal: Mapping[str, Any], role: str) -> dict[str, Any]:
    prefix = {"entry": "entry", "stop": "stop", "target": "target"}[role]
    nested = signal.get(f"{prefix}_observation")
    result = dict(nested) if isinstance(nested, Mapping) else {}
    aliases = {
        "observed_at": (f"{prefix}_observed_at", "observed_at", "source_timestamp"),
        "completed_at": (f"{prefix}_completed_at", "bar_completed_at", "completed_at"),
        "source": (f"{prefix}_source", "source", "preferred_source"),
        "source_url": (f"{prefix}_source_url", "source_url"),
        "source_hash": (f"{prefix}_source_hash", "source_hash_sha256", "enrichment_observation_sha256"),
        "observation_kind": (
            f"{prefix}_observation_kind",
            f"{prefix}_basis_kind",
            "observation_kind",
            "target_basis_kind" if role == "target" else "",
        ),
        "raw_value": (
            f"{prefix}_raw_value",
            f"{prefix}_basis_value",
            "raw_value",
            "target_basis_value" if role == "target" else "",
        ),
        "derivation_policy": (
            f"{prefix}_derivation_policy",
            "derivation_policy",
        ),
        "is_complete": (f"{prefix}_is_complete", "enrichment_is_complete", "is_complete"),
    }
    for field, names in aliases.items():
        if field in result and result[field] not in {None, ""}:
            continue
        for name in names:
            if signal.get(name) not in {None, ""}:
                result[field] = signal[name]
                break
    return result


def _valid_geometry(entry: float | None, stop: float | None, target: float | None, direction: str) -> bool:
    if entry is None or stop is None or target is None:
        return False
    if direction == "long":
        return target > entry > stop > 0
    return target < entry < stop and target > 0


def _parse_timestamp(value: Any) -> Any:
    try:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        parsed = datetime.fromisoformat(text)
        # A timestamp without an explicit offset has no safe interpretation at
        # this boundary.  Treating it as UTC can make a point-in-time plan
        # appear complete while silently shifting the source observation.
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _source_reference_valid(value: Any) -> bool:
    text = str(value or "").strip()
    # A public URL is preferred.  The only internal alternative is a bounded
    # internal:// source ID, and callers must still provide source identity,
    # completed timestamps, and a SHA-256 source hash for every observation.
    if _INTERNAL_SOURCE_URL.fullmatch(text):
        return True
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    normalized_hostname = hostname.lower().rstrip(".")
    if normalized_hostname == "localhost" or normalized_hostname.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        return True
    return (
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


def _first(value: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if value.get(name) not in {None, ""}:
            return value[name]
    return None


def _number(value: Any) -> float | None:
    try:
        if value in {None, ""}:
            return None
        result = float(str(value).replace("$", "").replace(",", "").replace("%", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


__all__ = [
    "ALLOWED_DERIVATION_POLICIES",
    "AlphaOpsMarketStructurePlan",
    "COMPLETE",
    "LEGACY_RESEARCH_BASELINE",
    "NO_VALID_PLAN",
    "PLAN_SCHEMA_VERSION",
    "PlanObservation",
    "SUPPORTED_TARGET_BASES",
    "SUPPORTED_OBSERVATION_KINDS",
    "apply_structural_level_enrichment",
    "build_alphaops_v5_plan",
    "build_market_structure_plan",
    "construct_alphaops_v5_plan",
    "is_valid_alphaops_v5_plan",
    "validate_alphaops_v5_plan",
    "with_structural_level_enrichment",
]
