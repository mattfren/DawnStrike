"""Immutable contracts for the R2 observation sidecar.

The sidecar has no decision, training, return, or broker surface.  It records
what was declared for a session and what a bounded source supplied.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCOPES = ("original_small_cap_gap", "liquid_reference_panel")
MEMBERSHIP = ("selected", "rejected", "unselected", "missing_input")
TEMPORAL_CLASSES = ("contemporaneous", "delayed", "unavailable")
STATUSES = (
    "CAPTURED",
    "EMPTY",
    "PARTIAL",
    "FAILED",
    "MISSED_SESSION",
    "BLOCKED",
    "STOPPED",
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def parse_utc(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    scope: str
    membership: str
    reason_codes: tuple[str, ...]
    required_inputs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, scope: str) -> UniverseEntry:
        symbol = str(value.get("symbol") or "").strip().upper()
        membership = str(value.get("membership") or "").strip().lower()
        if not symbol or any(ch.isspace() for ch in symbol):
            raise ValueError("universe entry symbol is required and must not contain whitespace")
        if scope not in SCOPES:
            raise ValueError(f"unsupported observation scope: {scope}")
        if membership not in MEMBERSHIP:
            raise ValueError(f"universe entry {symbol} has invalid membership")
        reasons = tuple(str(item) for item in value.get("reason_codes", ()) if str(item))
        inputs = tuple(str(item) for item in value.get("required_inputs", ()) if str(item))
        return cls(symbol, scope, membership, reasons, inputs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "scope": self.scope,
            "membership": self.membership,
            "reason_codes": list(self.reason_codes),
            "required_inputs": list(self.required_inputs),
        }


@dataclass(frozen=True)
class UniverseManifest:
    session_id: str
    market_date: str
    decision_deadline: datetime
    universe_generation_id: str
    source_config_sha256: str
    entries: tuple[UniverseEntry, ...]
    manifest_sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> UniverseManifest:
        if value.get("schema_version") != "dawnstrike.observation.universe.v1":
            raise ValueError("unsupported universe manifest schema")
        session_id = str(value.get("session_id") or "").strip()
        market_date = str(value.get("market_date") or "").strip()
        generation = str(value.get("universe_generation_id") or "").strip()
        source_hash = str(value.get("source_config_sha256") or "").strip()
        if not session_id or not market_date or not generation or len(source_hash) != 64:
            raise ValueError("universe manifest identity fields are incomplete")
        entries: list[UniverseEntry] = []
        scopes = value.get("scopes")
        if not isinstance(scopes, Mapping) or set(scopes) != set(SCOPES):
            raise ValueError("universe manifest must declare exactly the two named scopes")
        for scope in SCOPES:
            rows = scopes[scope]
            if not isinstance(rows, list):
                raise ValueError(f"scope {scope} must be a list")
            entries.extend(UniverseEntry.from_mapping(row, scope=scope) for row in rows)
        if len({(entry.scope, entry.symbol) for entry in entries}) != len(entries):
            raise ValueError("universe manifest contains duplicate scope/symbol entries")
        return cls(
            session_id=session_id,
            market_date=market_date,
            decision_deadline=parse_utc(value["decision_deadline"], label="decision_deadline"),
            universe_generation_id=generation,
            source_config_sha256=source_hash,
            entries=tuple(entries),
            manifest_sha256=sha256_json(value),
        )

    def census(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": self.session_id,
                "market_date": self.market_date,
                "universe_generation_id": self.universe_generation_id,
                "manifest_sha256": self.manifest_sha256,
                **entry.as_dict(),
            }
            for entry in self.entries
        ]


def event_id(
    *, session_id: str, scope: str, symbol: str, source: str, event_time: str, payload: Any
) -> str:
    return sha256_json(
        {
            "session_id": session_id,
            "scope": scope,
            "symbol": symbol,
            "source": source,
            "event_time": event_time,
            "payload": payload,
        }
    )


def event_temporal_class(*, available_at: datetime, deadline: datetime) -> str:
    return "contemporaneous" if available_at <= deadline else "delayed"
