"""Deterministic paper-trading episode identity and candidate de-duplication.

An episode is the smallest portfolio unit that may reserve capital.  Strategy
votes are metadata on that unit; they are not additional positions.  This
module is deliberately pure: it does not read or mutate a database, submit
orders, or manufacture missing market truth.
"""

from __future__ import annotations

import hashlib
import json
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
    def conflict_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.market_date,
            self.session_id,
            self.symbol,
            self.entry_window,
            self.frozen_plan_hash,
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


def build_episode_identity(candidate: Mapping[str, Any] | Any) -> EpisodeIdentity:
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

    def text(*names: str) -> str:
        for name in names:
            value = row.get(name)
            if value in (None, ""):
                value = payload.get(name)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    market_date = text("market_date", "trade_date", "date")[:10]
    session_id = text("session_id", "market_session", "session", "run_id", "scan_id")
    symbol = text("symbol", "ticker", "canonical_symbol").upper()
    direction = text("direction", "trade_direction").lower()
    entry_window = text(
        "entry_window",
        "entry_window_id",
        "entry_window_key",
        "decision_window",
    )
    frozen_hash = text(
        "frozen_plan_hash",
        "plan_hash",
        "plan_hash_sha256",
        "strategy_plan_hash",
    )
    if not frozen_hash:
        plan = row.get("frozen_plan") or row.get("plan")
        if plan is None:
            plan = payload.get("frozen_plan") or payload.get("plan")
        if isinstance(plan, Mapping):
            frozen_hash = _stable_hash(plan)
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
    if len(frozen_hash) < 16:
        raise EpisodeIdentityError("frozen_plan_hash is too short to be immutable evidence")
    basis = json.dumps(required, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return EpisodeIdentity(episode_id="episode:" + digest[:32], **required)


# Friendly aliases used by callers that describe the operation rather than the
# implementation detail.
episode_identity = build_episode_identity
canonical_episode_identity = build_episode_identity


def deduplicate_episode_candidates(
    candidates: Sequence[Mapping[str, Any] | Any],
) -> dict[str, Any]:
    """Collapse same-episode votes and block conflicting directions.

    The returned ``selected`` rows are deterministic representatives.  Each
    carries matched strategy metadata and the stable episode ID.  Rows with
    incomplete identity are blocked, never guessed.  ``counts`` explicitly
    distinguishes raw rows, unique symbols, unique episodes, and collapsed
    duplicates for audit artifacts.
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

    by_conflict: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for _key, rows in groups.items():
        identity = rows[0][1]
        by_conflict[identity.conflict_key].update(item[1].direction for item in rows)
    selected: list[dict[str, Any]] = []
    conflict_keys = {
        key for key, directions in by_conflict.items() if len(directions) > 1
    }
    conflict_episodes = len(conflict_keys)
    for _key, rows in sorted(groups.items(), key=lambda item: item[0]):
        identity = rows[0][1]
        directions = by_conflict[identity.conflict_key]
        ordered = sorted(rows, key=lambda item: _candidate_sort_key(item[0]))
        if len(directions) > 1:
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
        representative, _ = ordered[0]
        strategy_ids = sorted(
            {
                str(row.get("strategy_id") or "").strip()
                for row, _ident in ordered
                if str(row.get("strategy_id") or "").strip()
            }
        )
        primary = str(representative.get("primary_strategy_id") or "").strip() or (
            strategy_ids[0] if strategy_ids else ""
        )
        selected.append(
            {
                **representative,
                **identity.to_dict(),
                "matched_strategy_ids": strategy_ids,
                "primary_strategy_id": primary,
                "duplicate_count": max(0, len(ordered) - 1),
                "episode_decision": "SELECTED",
            }
        )
    symbols = {
        str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        for row in raw
        if str(row.get("symbol") or row.get("ticker") or "").strip()
    }
    unique_episode_count = len(selected) + sum(
        1 for directions in by_conflict.values() if len(directions) > 1
    )
    # A conflicting group has one logical candidate but no selected candidate;
    # keep that distinction in diagnostics and do not treat it as a duplicate.
    duplicate_collapse_count = sum(
        max(0, len(rows) - 1)
        for rows in groups.values()
        if len(by_conflict[rows[0][1].conflict_key]) == 1
    )
    return {
        "selected": selected,
        "blocked": blocked,
        "counts": {
            "raw_pair_count": len(raw),
            "unique_symbol_count": len(symbols),
            "unique_episode_count": unique_episode_count,
            "duplicate_collapse_count": duplicate_collapse_count,
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
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _candidate_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("rank") if row.get("rank") is not None else "999999"),
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_version") or ""),
        str(row.get("selection_id") or row.get("signal_id") or ""),
    )


__all__ = [
    "EpisodeIdentity",
    "EpisodeIdentityError",
    "build_episode_identity",
    "canonical_episode_identity",
    "deduplicate_episode_candidates",
    "episode_identity",
]
