"""Frozen, source-bound AlphaOps v5 market-structure plans.

The constructor is deliberately separate from reward/risk evaluation.  It
freezes levels from completed observations first; the cost and risk gates are
then free to reject the frozen plan.  In particular, a target is never moved
to make a reward/risk threshold pass.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from intraday_scanner.decisioning.contracts import canonical_json

NO_VALID_PLAN = "NO_VALID_PLAN"
COMPLETE = "COMPLETE"
LEGACY_RESEARCH_BASELINE = "LEGACY_RESEARCH_BASELINE"
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
_SHA256 = frozenset("0123456789abcdef")


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
        payload["target_frozen_before_reward_risk"] = self.is_complete
        return payload

    def compute_hash(self) -> str:
        """Return the constructor-assigned content hash."""

        return self.plan_hash_sha256

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())


def construct_alphaops_v5_plan(
    signal: Mapping[str, Any] | None = None,
    *,
    decision_at: str | None = None,
    direction: str = "long",
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
    normalized_direction = str(direction or signal.get("direction") or "long").lower()
    if normalized_direction not in {"long", "short"}:
        return _invalid(normalized_direction, "direction_unknown")
    declared = signal.get("market_structure_observations") or signal.get("plan_observations")
    observations: dict[str, Any] = dict(declared) if isinstance(declared, Mapping) else {}
    target_candidates = signal.get("target_observations") or signal.get("target_candidates")
    if isinstance(target_candidates, (list, tuple)):
        observations.setdefault("target_candidates", target_candidates)

    declared_entry = observations.get("entry")
    declared_stop = observations.get("stop")
    values = {
        "entry": _number(
            _first(
                signal,
                "entry",
                "entry_reference",
                "entry_watch_level",
                "entry_trigger",
                "breakout_trigger",
            )
            or (
                _first(declared_entry, "value", "price", "level")
                if isinstance(declared_entry, Mapping)
                else None
            )
        ),
        "stop": _number(
            _first(
                signal,
                "stop",
                "stop_price",
                "invalidation_level",
                "invalidation",
                "exit_line",
            )
            or (
                _first(declared_stop, "value", "price", "level")
                if isinstance(declared_stop, Mapping)
                else None
            )
        ),
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
            selected_basis = basis
            break
    if selected is None:
        return _invalid(normalized_direction, "target_observation_or_geometry_invalid")
    legs["target"] = selected

    entry = values["entry"]
    stop = values["stop"]
    target = selected.value
    if not _valid_geometry(entry, stop, target, normalized_direction):
        return _invalid(normalized_direction, "plan_geometry_invalid")
    frozen_at = str(decision_at or selected.completed_at or selected.observed_at)
    plan_payload = {
        "status": COMPLETE,
        "direction": normalized_direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "target_basis_kind": selected_basis,
        "target_frozen_at": frozen_at,
        "observations": {name: legs[name].to_dict() for name in ("entry", "stop", "target")},
        "research_only": True,
        "broker_execution_enabled": False,
    }
    plan_hash = hashlib.sha256(canonical_json(plan_payload).encode("utf-8")).hexdigest()
    return AlphaOpsMarketStructurePlan(
        status=COMPLETE,
        direction=normalized_direction,
        entry=entry,
        stop=stop,
        target=target,
        target_basis_kind=selected_basis,
        target_frozen_at=frozen_at,
        observations=tuple(legs[name] for name in ("entry", "stop", "target")),
        plan_hash_sha256=plan_hash,
    )


def build_alphaops_v5_plan(*args: Any, **kwargs: Any) -> AlphaOpsMarketStructurePlan:
    """Compatibility alias used by cycle callers and external audits."""

    return construct_alphaops_v5_plan(*args, **kwargs)


def build_market_structure_plan(*args: Any, **kwargs: Any) -> AlphaOpsMarketStructurePlan:
    """Generic alias for integrations that do not name the v5 policy."""

    return construct_alphaops_v5_plan(*args, **kwargs)


def _invalid(direction: str, reason: str) -> AlphaOpsMarketStructurePlan:
    payload = {
        "status": NO_VALID_PLAN,
        "direction": direction,
        "reason": reason,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return AlphaOpsMarketStructurePlan(
        status=NO_VALID_PLAN,
        direction=direction,
        entry=None,
        stop=None,
        target=None,
        target_basis_kind="",
        target_frozen_at="",
        observations=(),
        plan_hash_sha256=digest,
        reason=reason,
    )


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
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


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
    "AlphaOpsMarketStructurePlan",
    "COMPLETE",
    "LEGACY_RESEARCH_BASELINE",
    "NO_VALID_PLAN",
    "PlanObservation",
    "SUPPORTED_TARGET_BASES",
    "SUPPORTED_OBSERVATION_KINDS",
    "apply_structural_level_enrichment",
    "build_alphaops_v5_plan",
    "build_market_structure_plan",
    "construct_alphaops_v5_plan",
    "with_structural_level_enrichment",
]
