"""Deterministic paper-trading episode identity and candidate de-duplication.

An episode is the smallest portfolio unit that may reserve capital.  Strategy
votes are metadata on that unit; they are not additional positions.  This
module is deliberately pure: it does not read or mutate a database, submit
orders, or manufacture missing market truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class EpisodeIdentityError(ValueError):
    """Raised when a candidate cannot be bound to an immutable episode."""


@dataclass(frozen=True, slots=True)
class EpisodeIdentity:
    """The canonical fields and digest used for idempotent episode joins."""

    episode_id: str
    market_date: str
    session_id: str
    symbol: str
    direction: str
    entry_window: str
    frozen_plan_hash: str

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.market_date,
            self.session_id,
            self.symbol,
            self.direction,
            self.entry_window,
            self.frozen_plan_hash,
        )

    @property
    def conflict_key(self) -> tuple[str, str, str]:
        return (
            self.market_date,
            self.session_id,
            self.symbol,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "episode_id": self.episode_id,
            "market_date": self.market_date,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_window": self.entry_window,
            "frozen_plan_hash": self.frozen_plan_hash,
        }


def build_episode_identity(
    candidate: Mapping[str, Any] | Any,
    *,
    require_frozen: bool = True,
) -> EpisodeIdentity:
    """Build an identity from a selection/plan without strategy-specific fields.

    All identity fields are required.  A plan hash may be supplied directly or
    derived from an explicit ``plan``/``frozen_plan`` object.  Strategy ID,
    signal ID, rank, score, and retry timestamps intentionally do not enter the
    digest, so multi-strategy votes and retries join the same episode.
    """

    row = _mapping(candidate)
    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    payload = payload if isinstance(payload, Mapping) else {}
    # Selection persistence wraps the original Alpha signal under ``signal``.
    # Flatten that immutable payload for identity lookup so the watcher does
    # not silently downgrade a fully frozen signal to legacy merely because
    # the storage envelope added selection metadata around it.
    nested_signal = payload.get("signal")
    if isinstance(nested_signal, Mapping):
        payload = {**dict(payload), **dict(nested_signal)}

    def text(*names: str) -> str:
        for name in names:
            value = row.get(name)
            if value in (None, ""):
                value = payload.get(name)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    market_date = text("market_date", "trade_date", "date")[:10]
    # run_id/scan_id are retry/artifact identities, not stable market-session
    # identities.  Falling back to either would create a new episode on retry.
    session_id = text("session_id", "market_session", "session")
    symbol = text("symbol", "ticker", "canonical_symbol").upper()
    direction = text("direction", "trade_direction").lower()
    supplied_entry_window = text(
        "entry_window",
        "entry_window_id",
        "entry_window_key",
        "decision_window",
    )
    if supplied_entry_window:
        entry_window = _normalize_window(supplied_entry_window)
    else:
        entry_start = text("entry_window_start", "entry_start")
        entry_end = text("entry_window_end", "entry_end")
        if entry_start and entry_end:
            entry_window = _normalize_window(f"{entry_start}-{entry_end}")
        else:
            entry_window = ""
    plan = _frozen_plan(row, payload)
    if not direction and plan is not None:
        direction = str(plan.get("direction") or plan.get("trade_direction") or "").strip().lower()
    frozen_hash = text(
        "frozen_plan_hash",
        "plan_hash",
        "plan_hash_sha256",
        "strategy_plan_hash",
    )
    freeze_status = text(
        "plan_freeze_status",
        "freeze_status",
        "provenance_status",
        "plan_provenance_status",
    ).lower()
    if plan is None:
        raise EpisodeIdentityError(
            "episode identity requires a serialized frozen plan contract; "
            "hash-only identity is not trusted"
        )
    _validate_frozen_plan_contract(plan, row, payload)
    computed_hash = _frozen_plan_hash(plan)
    if not frozen_hash:
        frozen_hash = _declared_frozen_plan_hash(plan) or computed_hash
    required = {
        "market_date": market_date,
        "session_id": session_id,
        "symbol": symbol,
        "direction": direction,
        "entry_window": entry_window,
        "frozen_plan_hash": frozen_hash,
    }
    missing = tuple(name for name, value in required.items() if not value)
    if missing:
        raise EpisodeIdentityError(
            "episode identity is missing immutable fields: " + ", ".join(missing)
        )
    if direction not in {"long", "short"}:
        raise EpisodeIdentityError(f"episode direction is unsupported: {direction!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", frozen_hash):
        raise EpisodeIdentityError(
            "frozen_plan_hash must be a canonical lowercase 64-character SHA-256"
        )
    if computed_hash != frozen_hash:
        raise EpisodeIdentityError("frozen_plan does not match frozen_plan_hash")
    if _is_alphaops_plan(plan) and not freeze_status:
        # AlphaOps' COMPLETE contract carries the immutable freeze marker in
        # the serialized plan itself.  _signal_payload intentionally exposes
        # plan_levels_frozen/plan_construction_status rather than the generic
        # plan_freeze_status field, so do not reject valid Alpha payloads just
        # because they use that lane-specific vocabulary.
        if (
            str(plan.get("status") or "").strip().upper() == "COMPLETE"
            and plan.get("target_frozen_before_reward_risk") is True
        ):
            freeze_status = "frozen"
    if require_frozen and freeze_status not in {"frozen", "verified", "committed"}:
        raise EpisodeIdentityError(
            "episode identity requires frozen plan provenance status"
        )
    basis = json.dumps(required, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return EpisodeIdentity(episode_id="episode:" + digest[:32], **required)


# Friendly aliases used by callers that describe the operation rather than the
# implementation detail.
episode_identity = build_episode_identity
canonical_episode_identity = build_episode_identity


def _frozen_plan(row: Mapping[str, Any], payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Decode the self-verifying frozen-plan payload, rejecting opaque hashes."""

    plan: Any = row.get("frozen_plan")
    if plan in (None, ""):
        plan = row.get("plan")
    if plan in (None, ""):
        plan = row.get("alphaops_market_structure_plan")
    if plan in (None, ""):
        plan = payload.get("frozen_plan")
    if plan in (None, ""):
        plan = payload.get("plan")
    if plan in (None, ""):
        plan = payload.get("alphaops_market_structure_plan")
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except (TypeError, ValueError) as exc:
            raise EpisodeIdentityError("serialized frozen plan is not valid JSON") from exc
    if not isinstance(plan, Mapping) or not plan:
        return None
    return dict(plan)


def _validate_frozen_plan_contract(
    plan: Mapping[str, Any],
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    """Validate a plan using AlphaOps' public contract when available."""

    # AlphaOps v5 owns a richer market-structure contract.  Import lazily so
    # this identity utility remains usable by generic strategy fixtures while
    # accepting the canonical validator when that lane is present.
    if row.get("alphaops_market_structure_plan") is not None or payload.get(
        "alphaops_market_structure_plan"
    ) is not None:
        try:
            from intraday_scanner.alpha.plan_constructor import validate_alphaops_v5_plan
        except ImportError as exc:
            raise EpisodeIdentityError(
                "AlphaOps frozen plan validator is unavailable"
            ) from exc
        try:
            result = validate_alphaops_v5_plan(plan)
        except (TypeError, ValueError) as exc:
            raise EpisodeIdentityError(f"AlphaOps frozen plan is invalid: {exc}") from exc
        if result is False or (
            isinstance(result, Mapping)
            and str(result.get("status") or result.get("valid") or "").lower()
            in {"invalid", "false", "failed"}
        ):
            raise EpisodeIdentityError("AlphaOps frozen plan contract validation failed")
        if str(plan.get("status") or "").strip().upper() != "COMPLETE":
            raise EpisodeIdentityError("AlphaOps frozen plan is not complete")
        plan_direction = str(
            plan.get("direction") or plan.get("trade_direction") or ""
        ).strip().lower()
        candidate_direction = str(
            row.get("direction")
            or row.get("trade_direction")
            or payload.get("direction")
            or payload.get("trade_direction")
            or ""
        ).strip().lower()
        if candidate_direction and plan_direction and candidate_direction != plan_direction:
            raise EpisodeIdentityError("AlphaOps plan direction does not match episode direction")

    # Generic plans still need a recognized strict contract, not merely a
    # caller-recomputed hash.  Require frozen levels, source provenance, and a
    # committed freeze receipt before accepting a non-Alpha plan.
    else:
        _validate_generic_frozen_plan(plan)
        plan_direction = str(plan.get("direction") or plan.get("trade_direction") or "").lower()
        candidate_direction = str(
            row.get("direction")
            or row.get("trade_direction")
            or payload.get("direction")
            or payload.get("trade_direction")
            or ""
        ).strip().lower()
        if candidate_direction and candidate_direction != plan_direction:
            raise EpisodeIdentityError("frozen plan direction does not match episode direction")

    # All plans need deterministic serialization.  This round-trip
    # rejects NaN/Infinity and values that cannot be represented immutably;
    # the digest below is the only accepted identity hash.
    try:
        encoded = json.dumps(
            plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise EpisodeIdentityError("frozen plan contract is not canonically serializable") from exc
    if not isinstance(decoded, Mapping) or not decoded:
        raise EpisodeIdentityError("frozen plan contract must be a non-empty object")


def _is_alphaops_plan(plan: Mapping[str, Any]) -> bool:
    return (
        str(plan.get("schema_version") or "").strip().lower()
        == "dawnstrike.alphaops_market_structure_plan.v1"
    )


def _declared_frozen_plan_hash(plan: Mapping[str, Any]) -> str:
    """Return a contract-declared plan hash, when a strict lane provides one."""

    if not _is_alphaops_plan(plan):
        return ""
    return str(plan.get("plan_hash_sha256") or "").strip().lower()


def _frozen_plan_hash(plan: Mapping[str, Any]) -> str:
    """Compute the hash defined by the recognized frozen-plan contract."""

    if _is_alphaops_plan(plan):
        # AlphaOps hashes the emitted serialized contract excluding the hash
        # field.  Hashing the full mapping would make every valid Alpha plan
        # fail identity verification because it would hash the digest into
        # itself.
        payload = dict(plan)
        payload.pop("plan_hash_sha256", None)
        return _stable_hash(payload)
    return _stable_hash(plan)


def _validate_generic_frozen_plan(plan: Mapping[str, Any]) -> None:
    schema = str(plan.get("schema_version") or plan.get("contract") or "").strip().lower()
    recognized_prefix = schema.startswith(("dawnstrike.", "paperops.", "paper_ops."))
    if not schema or not recognized_prefix or not (
        "frozen_plan" in schema
        or schema.endswith("episode_plan.v1")
        or schema.endswith("paper_plan.v1")
    ):
        raise EpisodeIdentityError(
            "generic frozen plan must use a recognized strict schema"
        )
    levels = plan.get("levels")
    levels = levels if isinstance(levels, Mapping) else plan
    aliases = {
        "entry": ("entry", "entry_price", "entry_reference"),
        "stop": ("stop", "stop_price"),
        "target": ("target", "target_price"),
    }
    numeric_levels: dict[str, float] = {}
    for name, keys in aliases.items():
        value = next((levels.get(key) for key in keys if levels.get(key) is not None), None)
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise EpisodeIdentityError(
                f"generic frozen plan is missing frozen {name} level"
            ) from exc
        if not math.isfinite(number):
            raise EpisodeIdentityError(f"generic frozen plan {name} level is not finite")
        numeric_levels[name] = number

    direction = str(plan.get("direction") or plan.get("trade_direction") or "").lower()
    if direction not in {"long", "short"}:
        raise EpisodeIdentityError("generic frozen plan requires frozen direction")
    if direction == "long" and not (
        numeric_levels["stop"] < numeric_levels["entry"] < numeric_levels["target"]
    ):
        raise EpisodeIdentityError("generic frozen plan levels do not match long direction")
    if direction == "short" and not (
        numeric_levels["target"] < numeric_levels["entry"] < numeric_levels["stop"]
    ):
        raise EpisodeIdentityError("generic frozen plan levels do not match short direction")

    provenance = plan.get("provenance") or plan.get("source_provenance") or {}
    if not isinstance(provenance, Mapping):
        provenance = plan
    source_hash = str(
        provenance.get("source_hash_sha256")
        or provenance.get("source_artifact_hash_sha256")
        or plan.get("source_hash_sha256")
        or ""
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise EpisodeIdentityError(
            "generic frozen plan requires canonical source provenance hash"
        )
    receipt = (
        plan.get("freeze_receipt")
        or plan.get("provenance_receipt")
        or plan.get("receipt")
    )
    if not isinstance(receipt, Mapping):
        raise EpisodeIdentityError("generic frozen plan requires a freeze receipt")
    receipt_status = str(receipt.get("status") or "").strip().lower()
    receipt_hash = str(receipt.get("receipt_sha256") or "").strip()
    if receipt_status not in {"frozen", "committed", "verified"} or not re.fullmatch(
        r"[0-9a-f]{64}", receipt_hash
    ):
        raise EpisodeIdentityError("generic frozen plan freeze receipt is not committed")
    receipt_body = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    try:
        expected_receipt_hash = _stable_hash(receipt_body)
    except (TypeError, ValueError) as exc:
        raise EpisodeIdentityError("generic frozen plan freeze receipt is not canonical") from exc
    if expected_receipt_hash != receipt_hash:
        raise EpisodeIdentityError("generic frozen plan freeze receipt hash mismatch")


def deduplicate_episode_candidates(
    candidates: Sequence[Mapping[str, Any] | Any],
) -> dict[str, Any]:
    """Collapse same-episode votes and enforce one symbol reservation.

    The returned ``selected`` rows are deterministic representatives.  Each
    carries matched strategy metadata and the stable episode ID.  Rows with
    incomplete identity are blocked, never guessed.  ``counts`` explicitly
    distinguishes raw rows, unique symbols, immutable episode identities,
    reservation candidates, and collapsed duplicates for audit artifacts.
    """

    raw = [_mapping(item) for item in candidates]
    groups: dict[tuple[str, ...], list[tuple[dict[str, Any], EpisodeIdentity]]] = defaultdict(list)
    blocked: list[dict[str, Any]] = []
    for row in raw:
        try:
            identity = build_episode_identity(row)
        except EpisodeIdentityError as exc:
            blocked.append({**row, "episode_decision": "BLOCKED", "blocked_reason": str(exc)})
            continue
        groups[identity.key].append((row, identity))

    # Group immutable identity candidates by market/session/symbol.  The
    # frozen plan hash deliberately remains in EpisodeIdentity.key, but it is
    # not a reservation boundary: competing plans for one overlapping symbol
    # must still reserve only once.
    by_conflict: dict[
        tuple[str, ...], list[tuple[dict[str, Any], EpisodeIdentity]]
    ] = defaultdict(list)
    for rows in groups.values():
        by_conflict[rows[0][1].conflict_key].extend(rows)

    conflict_group_keys: set[tuple[str, ...]] = set()
    conflict_keys: set[tuple[str, ...]] = set()
    for conflict_key, rows in by_conflict.items():
        for left_index, (_left_row, left) in enumerate(rows):
            for _right_row, right in rows[left_index + 1 :]:
                if left.direction != right.direction and _windows_overlap(
                    left.entry_window, right.entry_window
                ):
                    conflict_keys.add(conflict_key)
                    conflict_group_keys.update((left.key, right.key))

    # Connected components of overlapping same-direction identities form one
    # portfolio reservation.  This handles A overlaps B overlaps C without
    # letting an ordering artifact create two positions.
    reservation_components: list[list[tuple[str, ...]]] = []
    component_by_key: dict[tuple[str, ...], int] = {}
    for _conflict_key, rows in sorted(by_conflict.items(), key=lambda item: item[0]):
        keys = sorted({identity.key for _row, identity in rows})
        for key in keys:
            component_by_key[key] = len(reservation_components)
            reservation_components.append([key])
        for left_index, left_key in enumerate(keys):
            left = rows[
                next(index for index, (_row, ident) in enumerate(rows) if ident.key == left_key)
            ][1]
            for right_key in keys[left_index + 1 :]:
                right = rows[
                    next(
                        index
                        for index, (_row, ident) in enumerate(rows)
                        if ident.key == right_key
                    )
                ][1]
                if left.direction == right.direction and _windows_overlap(
                    left.entry_window, right.entry_window
                ):
                    left_component = component_by_key[left_key]
                    right_component = component_by_key[right_key]
                    if left_component != right_component:
                        merged = [
                            *reservation_components[left_component],
                            *reservation_components[right_component],
                        ]
                        reservation_components[left_component] = merged
                        reservation_components[right_component] = []
                        for member in merged:
                            component_by_key[member] = left_component

    selected: list[dict[str, Any]] = []
    conflict_episodes = len(conflict_keys)
    overlap_collapse_count = 0
    for component in reservation_components:
        if not component:
            continue
        grouped_rows = [item for key in component for item in groups[key]]
        ordered = sorted(grouped_rows, key=lambda item: _candidate_sort_key(item[0]))
        identities = [identity for _row, identity in ordered]
        if any(identity.key in conflict_group_keys for identity in identities):
            for row, ident in ordered:
                blocked.append(
                    {
                        **row,
                        **ident.to_dict(),
                        "episode_decision": "BLOCKED",
                        "blocked_reason": "conflicting_direction_candidates",
                    }
                )
            continue
        representative, identity = ordered[0]
        strategy_ids = sorted(
            {
                str(row.get("strategy_id") or "").strip()
                for row, _ident in ordered
                if str(row.get("strategy_id") or "").strip()
            }
        )
        episode_ids = sorted({ident.episode_id for ident in identities})
        primary = str(representative.get("primary_strategy_id") or "").strip() or (
            strategy_ids[0] if strategy_ids else ""
        )
        exact_duplicate_count = sum(
            max(0, len(groups[key]) - 1) for key in component
        )
        overlap_collapse_count += max(0, len(component) - 1)
        selected.append(
            {
                **representative,
                **identity.to_dict(),
                "matched_strategy_ids": strategy_ids,
                "primary_strategy_id": primary,
                "matched_episode_ids": episode_ids,
                "alternative_episode_ids": [
                    item for item in episode_ids if item != identity.episode_id
                ],
                "alternative_strategy_ids": [item for item in strategy_ids if item != primary],
                "duplicate_count": exact_duplicate_count,
                "episode_decision": "SELECTED",
            }
        )
    symbols = {
        str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        for row in raw
        if str(row.get("symbol") or row.get("ticker") or "").strip()
    }
    unique_episode_count = len(groups)
    # A conflicting group has one logical candidate but no selected candidate;
    # keep that distinction in diagnostics and do not treat it as a duplicate.
    duplicate_collapse_count = sum(
        max(0, len(rows) - 1)
        for rows in groups.values()
        if rows[0][1].key not in conflict_group_keys
    )
    return {
        "selected": selected,
        "blocked": blocked,
        "counts": {
            "raw_pair_count": len(raw),
            "unique_symbol_count": len(symbols),
            "unique_episode_count": unique_episode_count,
            "unique_reservation_count": len(selected),
            "duplicate_collapse_count": duplicate_collapse_count,
            "overlapping_reservation_collapse_count": overlap_collapse_count,
            "conflicting_direction_episode_count": conflict_episodes,
            "blocked_count": len(blocked),
        },
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    return dict(getattr(value, "__dict__", {}))


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _candidate_sort_key(row: Mapping[str, Any]) -> tuple[float, str, str, str, str, str]:
    rank = row.get("rank")
    try:
        rank_value = float(rank) if rank not in (None, "") else 999999.0
    except (TypeError, ValueError):
        rank_value = 999999.0
    if not math.isfinite(rank_value):
        rank_value = 999999.0
    return (
        rank_value,
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_version") or ""),
        str(row.get("selection_id") or row.get("signal_id") or ""),
        str(row.get("episode_id") or ""),
        str(row.get("frozen_plan_hash") or row.get("plan_hash_sha256") or ""),
    )


def _normalize_window(value: str) -> str:
    """Normalize and validate a market entry window, failing closed on labels."""

    bounds = _window_bounds(value)
    if bounds is None:
        raise EpisodeIdentityError(
            "entry_window must be a parseable HH:MM-HH:MM interval"
        )
    start, end = bounds
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def _window_bounds(value: str) -> tuple[int, int] | None:
    matches = re.fullmatch(
        r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*",
        value,
    )
    if matches is None:
        return None
    start_hour, start_minute, end_hour, end_minute = (
        int(matches.group(index)) for index in range(1, 5)
    )
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        return None
    if not (0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        return None
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    return (start, end) if end > start else None


def _windows_overlap(left: str, right: str) -> bool:
    """Compare already validated HH:MM-HH:MM windows."""

    left_bounds = _window_bounds(left)
    right_bounds = _window_bounds(right)
    if left_bounds is None or right_bounds is None:
        # Identity construction validates both; this branch protects direct
        # helper use and deliberately treats malformed data as overlapping.
        return True
    return left_bounds[0] < right_bounds[1] and right_bounds[0] < left_bounds[1]


__all__ = [
    "EpisodeIdentity",
    "EpisodeIdentityError",
    "build_episode_identity",
    "canonical_episode_identity",
    "deduplicate_episode_candidates",
    "episode_identity",
]
