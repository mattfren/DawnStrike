"""Governed Morning-to-PaperOps point-in-time universe handoff.

The Morning cycle is the authority for the symbols that a scheduled PaperOps
run may observe.  This module turns the already persisted, governed Morning
artifacts into one immutable, content-addressed contract.  It intentionally
does not discover symbols, infer membership, or turn unavailable provider
truth into rows.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from intraday_scanner.services.luna_core_universe_service import (
    _canonical_member_hash,
    canonical_symbol,
)
from intraday_scanner.services.luna_core_universe_service import (
    _hash as _core_hash,
)
from intraday_scanner.v2.strategies import build_strategy_catalog

SCHEMA_VERSION = "dawnstrike.paperops.universe_handoff.v1"
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SOURCE_STATUSES = {"success", "no_data", "empty", "partial", "failed"}


class UniverseHandoffError(ValueError):
    """Raised when a Morning universe handoff cannot be trusted."""


def build_universe_handoff(
    morning_root: str | Path,
    market_date: str | date,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build and persist the exact deduplicated Morning PIT union.

    A PARTIAL contract is valid when one provider lane is unavailable; its
    shortfall is explicit and the scheduled consumer can still run the
    symbols actually backed by the surviving lane.  An empty union is never
    valid.
    """

    root = Path(morning_root).resolve()
    requested_date = _date_text(market_date)
    if requested_date is None:
        raise UniverseHandoffError("invalid market date")
    cycle = _read_required_json(root / "alpha_cycle.json", "alpha cycle")
    cycle_contract = _read_required_json(root / "alpha_run_contract.json", "alpha run contract")
    core = _read_required_json(root / "core_universe_contract.json", "core universe contract")
    source_summary = _read_required_json(
        root / "web_collect" / "source_summary.json", "Morning source summary"
    )

    _validate_cycle_identity(cycle, cycle_contract, requested_date)
    _validate_core_contract(core, requested_date)
    _validate_source_summary(source_summary, requested_date)

    source_snapshot = _resolve_source_path(
        root,
        str(source_summary.get("snapshot_path") or ""),
    )
    mover_rows = _read_mover_snapshot(source_snapshot, requested_date)
    source_status = str(source_summary.get("status") or "").strip().lower()
    mover_lane_status = str(source_summary.get("mover_lane_status") or "").strip().upper()
    mover_available = source_status in {"success", "partial"} and bool(mover_rows)
    if mover_lane_status == "SOURCE_FAILED":
        mover_available = False

    core_ready = str(core.get("status") or "").strip().upper() == "READY"
    core_members = _core_members(core) if core_ready else []
    mover_members = (
        _mover_members(mover_rows, source_summary, requested_date) if mover_available else []
    )
    members = _merge_members(core_members, mover_members)
    symbols = [str(row["symbol"]) for row in members]
    if not symbols:
        raise UniverseHandoffError("universe union is empty; no current provider truth")

    shortfalls: list[str] = []
    if not core_ready:
        shortfalls.append("core_membership_unavailable")
    if not mover_available:
        shortfalls.append("governed_mover_source_unavailable")
    if source_status in {"success", "partial"} and not mover_rows:
        shortfalls.append("governed_mover_snapshot_empty")
    declared_mover_count = int(source_summary.get("candidate_count") or 0)
    if source_status in {"success", "partial"} and declared_mover_count != len(mover_members):
        shortfalls.append("governed_mover_snapshot_count_mismatch")
    if int(source_summary.get("source_failures") or 0) > 0:
        shortfalls.append("provider_failures_present")
    coverage_status = "COMPLETE" if not shortfalls else "PARTIAL"

    strategy_ids = _expected_strategy_ids()
    adapter = source_summary.get("morning_strategy_adapter")
    declared_strategy_ids = (
        sorted(
            {
                str(value)
                for value in adapter.get("enabled_strategy_ids") or []
                if str(value).strip()
            }
        )
        if isinstance(adapter, dict)
        else []
    )
    generated_at = _first_text(
        cycle_contract.get("generated_at"),
        cycle.get("generated_at"),
        source_summary.get("created_at"),
    )
    if _iso_date(generated_at) != requested_date:
        raise UniverseHandoffError("Morning artifacts are stale or cross-date")

    source_artifacts = [
        _artifact(root / "alpha_cycle.json", root, "alpha_cycle"),
        _artifact(root / "alpha_run_contract.json", root, "alpha_run_contract"),
        _artifact(root / "core_universe_contract.json", root, "core_universe_contract"),
        _artifact(root / "web_collect" / "source_summary.json", root, "mover_source_summary"),
        _artifact(source_snapshot, root, "mover_snapshot"),
    ]
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "market_date": requested_date,
        "generated_at": generated_at,
        "run_id": str(cycle.get("scan_id") or cycle_contract.get("producer_run_id") or ""),
        "morning_scan_id": str(cycle.get("scan_id") or ""),
        "universe_symbols": symbols,
        "symbols": symbols,
        "members": members,
        "core_universe": {
            "status": str(core.get("status") or "DATA_UNAVAILABLE"),
            "contract_id": str(core.get("contract_id") or ""),
            "content_hash_sha256": str(core.get("content_hash_sha256") or ""),
            "canonical_member_set_hash_sha256": str(
                core.get("canonical_member_set_hash_sha256") or ""
            ),
            "requested_market_date": core.get("requested_market_date"),
            "membership_count": int(core.get("membership_count") or 0),
            "included_count": len(core_members),
        },
        "mover_source": {
            "status": source_status,
            "lane_status": mover_lane_status or ("AVAILABLE" if mover_available else "UNAVAILABLE"),
            "source_identity": str(
                source_summary.get("source_identity") or source_summary.get("run_id") or ""
            ),
            "snapshot_path": source_snapshot.relative_to(root).as_posix(),
            "declared_count": int(source_summary.get("candidate_count") or 0),
            "available_count": len(mover_members),
            "source_failures": int(source_summary.get("source_failures") or 0),
        },
        "coverage": {
            "status": coverage_status,
            "core_membership_count": int(core.get("membership_count") or 0),
            "core_included_count": len(core_members),
            "mover_declared_count": int(source_summary.get("candidate_count") or 0),
            "mover_included_count": len(mover_members),
            "union_count": len(symbols),
            "overlap_count": sum(
                1 for row in members if set(row.get("lanes") or []) == {"core", "mover"}
            ),
            "shortfall_reasons": sorted(set(shortfalls)),
        },
        "strategy_fleet": {
            "expected_strategy_ids": list(strategy_ids),
            "declared_morning_strategy_ids": declared_strategy_ids,
            "missing_declared_strategy_ids": sorted(set(strategy_ids) - set(declared_strategy_ids)),
            "expected_count": len(strategy_ids),
        },
        "safety": {
            "research_only": True,
            "broker_execution": "disabled",
            "broker_execution_enabled": False,
            "missing_truth_is_zero": False,
        },
        "source_artifacts": source_artifacts,
    }
    digest = _handoff_hash(body)
    body["content_hash_sha256"] = digest
    body["content_hash"] = digest
    body["handoff_id"] = "paperops-universe-" + digest[:24]
    body["universe_id"] = "paperops-pit-universe-" + digest[:24]
    if output_path is not None:
        _write_immutable(Path(output_path), body)
    return body


def load_universe_handoff(
    path: str | Path,
    *,
    market_date: str | date | None = None,
    require_production: bool = False,
    verify_sources: bool = True,
) -> dict[str, Any]:
    """Load and verify a content-addressed universe handoff."""

    handoff_path = Path(path).resolve()
    payload = _read_required_json(handoff_path, "PaperOps universe handoff")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise UniverseHandoffError("universe handoff schema is invalid")
    expected_date = _date_text(market_date) if market_date is not None else None
    actual_date = _date_text(payload.get("market_date"))
    if actual_date is None or (expected_date is not None and actual_date != expected_date):
        raise UniverseHandoffError("universe handoff market date conflicts")
    if require_production and not verify_sources:
        raise UniverseHandoffError("production universe handoff requires source verification")
    claimed = str(payload.get("content_hash_sha256") or "").lower()
    if not _SHA_PATTERN.fullmatch(claimed) or claimed != _handoff_hash(payload):
        raise UniverseHandoffError("universe handoff content hash is invalid")
    if payload.get("content_hash") != claimed:
        raise UniverseHandoffError("universe handoff content hash alias conflicts")
    if payload.get("handoff_id") != "paperops-universe-" + claimed[:24]:
        raise UniverseHandoffError("universe handoff identity conflicts")
    if payload.get("universe_id") != "paperops-pit-universe-" + claimed[:24]:
        raise UniverseHandoffError("universe handoff universe identity conflicts")
    symbols = _validate_symbols(payload.get("universe_symbols"), "universe_symbols")
    if symbols != _validate_symbols(payload.get("symbols"), "symbols"):
        raise UniverseHandoffError("universe handoff symbol aliases conflict")
    members = payload.get("members")
    if (
        not isinstance(members, list)
        or [str(row.get("symbol")) for row in members if isinstance(row, dict)] != symbols
    ):
        raise UniverseHandoffError("universe handoff member union is invalid")
    _validate_core_binding(payload, actual_date)
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict) or str(coverage.get("status") or "") not in {
        "COMPLETE",
        "PARTIAL",
    }:
        raise UniverseHandoffError("universe handoff coverage is invalid")
    safety = payload.get("safety")
    if require_production and (
        not isinstance(safety, dict)
        or safety.get("research_only") is not True
        or safety.get("broker_execution") != "disabled"
        or safety.get("broker_execution_enabled") is not False
        or safety.get("missing_truth_is_zero") is not False
    ):
        raise UniverseHandoffError("universe handoff production safety binding is invalid")
    if require_production and not symbols:
        raise UniverseHandoffError("universe handoff has no symbols")
    fleet = payload.get("strategy_fleet")
    expected_strategy_ids = list(_expected_strategy_ids())
    if (
        not isinstance(fleet, dict)
        or fleet.get("expected_strategy_ids") != expected_strategy_ids
        or fleet.get("expected_count") != len(expected_strategy_ids)
    ):
        raise UniverseHandoffError("universe handoff strategy fleet is invalid")
    if verify_sources:
        artifacts = payload.get("source_artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise UniverseHandoffError("universe handoff source artifacts are missing")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise UniverseHandoffError("universe handoff source artifact is malformed")
            artifact_path = _resolve_source_path(
                handoff_path.parent, str(artifact.get("path") or "")
            )
            digest = str(artifact.get("sha256") or "").lower()
            if not _SHA_PATTERN.fullmatch(digest) or _sha256_file(artifact_path) != digest:
                raise UniverseHandoffError("universe handoff source artifact hash mismatch")

        # The handoff's content hash authenticates the bytes of the handoff,
        # but it does not by itself authenticate the meaning of derived fields
        # such as the union, member lanes, and coverage.  Rebuild the contract
        # from the exact hashed Morning inputs and compare the complete
        # unhashed semantic body.  This closes the self-consistent-forgery
        # case where an attacker replaces those fields and recomputes every
        # handoff digest and identity alias.
        expected = build_universe_handoff(handoff_path.parent, actual_date)
        expected_artifacts = expected.get("source_artifacts")
        if artifacts != expected_artifacts:
            raise UniverseHandoffError("universe handoff source artifact manifest is invalid")
        identity_fields = {
            "content_hash_sha256",
            "content_hash",
            "handoff_id",
            "universe_id",
        }
        expected_body = {
            key: value for key, value in expected.items() if key not in identity_fields
        }
        actual_body = {key: value for key, value in payload.items() if key not in identity_fields}
        if actual_body != expected_body:
            raise UniverseHandoffError("universe handoff semantic binding is invalid")
    return payload


def validate_universe_handoff(
    path: str | Path, market_date: str | date | None = None
) -> dict[str, Any]:
    return load_universe_handoff(path, market_date=market_date, require_production=True)


def _validate_cycle_identity(
    cycle: dict[str, Any], contract: dict[str, Any], market_date: str
) -> None:
    if str(contract.get("schema_version") or "") != "alphaops.run_contract.v1":
        raise UniverseHandoffError("Morning run contract schema is invalid")
    if str(contract.get("producer") or "") != "alphaops":
        raise UniverseHandoffError("Morning run contract producer is invalid")
    if str(contract.get("market_date") or "") != market_date:
        raise UniverseHandoffError("Morning run contract market date conflicts")
    scan_id = str(cycle.get("scan_id") or "")
    if not scan_id or scan_id != str(contract.get("producer_run_id") or ""):
        raise UniverseHandoffError("Morning scan identity is missing or inconsistent")
    cycle_date = _iso_date(cycle.get("generated_at"))
    if cycle_date is not None and cycle_date != market_date:
        raise UniverseHandoffError("Morning cycle artifact is stale or cross-date")
    if str(contract.get("source_status") or "") not in {
        "success",
        "ok",
        "no_data",
        "partial",
        "failed",
    }:
        raise UniverseHandoffError("Morning source status is not governed")


def _validate_core_contract(core: dict[str, Any], market_date: str) -> None:
    if str(core.get("schema_version") or "") != "dawnstrike.luna.core_universe.v1":
        raise UniverseHandoffError("core universe contract schema is invalid")
    if str(core.get("requested_market_date") or "") != market_date:
        raise UniverseHandoffError("core universe contract market date conflicts")
    if _iso_date(core.get("observed_at")) != market_date:
        raise UniverseHandoffError("core universe contract is stale or cross-date")
    claimed = str(core.get("content_hash_sha256") or "").lower()
    if not _SHA_PATTERN.fullmatch(claimed):
        raise UniverseHandoffError("core universe contract hash is missing")
    unhashed = dict(core)
    for field in ("content_hash_sha256", "content_hash", "contract_id", "universe_id"):
        unhashed.pop(field, None)
    if _core_hash(unhashed) != claimed:
        raise UniverseHandoffError("core universe contract hash mismatch")
    if core.get("content_hash") not in {None, "", claimed}:
        raise UniverseHandoffError("core universe content hash alias conflicts")
    members = core.get("members")
    if not isinstance(members, list):
        raise UniverseHandoffError("core universe members are missing")
    core_symbols = [row.get("symbol") for row in members if isinstance(row, dict)]
    if core_symbols:
        _validate_symbols(core_symbols, "core members")
    elif str(core.get("status") or "").upper() == "READY":
        raise UniverseHandoffError("READY core universe has no members")
    declared_member_hash = str(core.get("canonical_member_set_hash_sha256") or "").lower()
    if not _SHA_PATTERN.fullmatch(declared_member_hash):
        raise UniverseHandoffError("core universe canonical member hash is missing")
    canonical_records = [
        {
            "symbol": canonical_symbol(row.get("symbol") or row.get("ticker")),
            "provider_symbol": canonical_symbol(
                row.get("provider_symbol") or row.get("mapped_symbol") or row.get("symbol")
            ),
            "asset_class": str(row.get("asset_class") or row.get("security_type") or "common_stock")
            .strip()
            .lower(),
            "index": str(index),
            "valid_from": row.get("valid_from"),
            "valid_to": row.get("valid_to"),
        }
        for row in members
        if isinstance(row, dict)
        for index in (row.get("index_memberships") or [])
    ]
    if _canonical_member_hash(canonical_records) != declared_member_hash:
        raise UniverseHandoffError("core universe canonical member hash mismatch")


def _validate_source_summary(source: dict[str, Any], market_date: str) -> None:
    status = str(source.get("status") or "").strip().lower()
    if status not in _ALLOWED_SOURCE_STATUSES:
        raise UniverseHandoffError("Morning source summary status is invalid")
    requested = str(source.get("requested_observed_at") or "").strip()
    if not requested or _iso_date(requested) != market_date:
        raise UniverseHandoffError("Morning source summary is cross-date")
    if not str(source.get("snapshot_path") or "").strip():
        raise UniverseHandoffError("Morning source summary snapshot path is missing")


def _validate_core_binding(payload: dict[str, Any], market_date: str) -> None:
    core = payload.get("core_universe")
    if not isinstance(core, dict):
        raise UniverseHandoffError("universe handoff core binding is missing")
    if str(core.get("requested_market_date") or "") != market_date:
        raise UniverseHandoffError("universe handoff core binding is cross-date")
    if str(core.get("status") or "") == "READY" and not _SHA_PATTERN.fullmatch(
        str(core.get("content_hash_sha256") or "")
    ):
        raise UniverseHandoffError("universe handoff core hash is invalid")


def _expected_strategy_ids() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(strategy.strategy_id)
                for strategy in build_strategy_catalog()
                if str(strategy.strategy_id)
                not in {"benchmark_buy_hold_equal_weight", "baseline_buy_hold"}
            }
        )
    )


def _core_members(core: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in core.get("members") or []:
        if not isinstance(raw, dict):
            continue
        symbol = canonical_symbol(raw.get("symbol") or raw.get("ticker"))
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            continue
        memberships = sorted(
            {str(item).strip() for item in raw.get("index_memberships") or [] if str(item).strip()}
        )
        output.append(
            {
                "symbol": symbol,
                "lanes": ["core"],
                "lane": "core",
                "index_memberships": memberships,
                "sources": sorted({str(item) for item in raw.get("sources") or [] if str(item)}),
                "member_lineage": {
                    "contract_id": str(core.get("contract_id") or ""),
                    "contract_hash_sha256": str(core.get("content_hash_sha256") or ""),
                    "canonical_member_set_hash_sha256": str(
                        core.get("canonical_member_set_hash_sha256") or ""
                    ),
                },
            }
        )
    return sorted(output, key=lambda row: row["symbol"])


def _mover_members(
    rows: Iterable[dict[str, Any]], source: dict[str, Any], market_date: str
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = canonical_symbol(row.get("ticker") or row.get("symbol"))
        if _SYMBOL_PATTERN.fullmatch(symbol):
            grouped.setdefault(symbol, []).append(dict(row, ticker=symbol))
    output: list[dict[str, Any]] = []
    for symbol, candidates in sorted(grouped.items()):
        selected = sorted(candidates, key=lambda row: json.dumps(row, sort_keys=True, default=str))[
            0
        ]
        output.append(
            {
                "symbol": symbol,
                "lanes": ["mover"],
                "lane": "mover",
                "index_memberships": [],
                "sources": sorted({str(row.get("source") or "unknown") for row in candidates}),
                "member_lineage": {
                    "source_identity": str(
                        source.get("source_identity") or source.get("run_id") or ""
                    ),
                    "source_status": str(source.get("status") or ""),
                    "market_date": market_date,
                    "source_row": selected,
                },
            }
        )
    return output


def _merge_members(
    core: Iterable[dict[str, Any]], movers: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in [*core, *movers]:
        symbol = str(item["symbol"])
        existing = merged.get(symbol)
        if existing is None:
            merged[symbol] = dict(item)
            continue
        existing["lanes"] = sorted(set(existing.get("lanes") or []) | set(item.get("lanes") or []))
        existing["lane"] = "+".join(existing["lanes"])
        existing["index_memberships"] = sorted(
            set(existing.get("index_memberships") or []) | set(item.get("index_memberships") or [])
        )
        existing["sources"] = sorted(
            set(existing.get("sources") or []) | set(item.get("sources") or [])
        )
        lineage = existing.setdefault("member_lineage", {})
        lineage.update(item.get("member_lineage") or {})
    return [merged[symbol] for symbol in sorted(merged)]


def _read_mover_snapshot(path: Path, market_date: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise UniverseHandoffError("governed mover snapshot is missing")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        raise UniverseHandoffError("governed mover snapshot is unreadable") from exc
    for row in rows:
        declared = str(row.get("market_date") or row.get("as_of_date") or "").strip()
        if declared and declared != market_date:
            raise UniverseHandoffError("governed mover snapshot is cross-date")
    return rows


def _validate_symbols(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, list):
        raise UniverseHandoffError(f"{label} are missing")
    symbols = [canonical_symbol(item) for item in raw]
    if not symbols or any(not _SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise UniverseHandoffError(f"{label} contain invalid symbols")
    if symbols != sorted(set(symbols)):
        raise UniverseHandoffError(f"{label} are not exact deduplicated order")
    return symbols


def _artifact(path: Path, root: Path, kind: str) -> dict[str, Any]:
    if not path.is_file():
        raise UniverseHandoffError(f"required {kind} artifact is missing")
    return {
        "kind": kind,
        "path": path.resolve().relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
    }


def _resolve_source_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise UniverseHandoffError("source artifact path escapes Morning artifact root") from exc
    return resolved


def _read_required_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise UniverseHandoffError(f"{label} is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UniverseHandoffError(f"{label} is malformed") from exc
    if not isinstance(payload, dict):
        raise UniverseHandoffError(f"{label} must be an object")
    return payload


def _write_immutable(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise UniverseHandoffError("universe handoff conflicts with immutable same-day bytes")
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def _handoff_hash(payload: dict[str, Any]) -> str:
    unhashed = {
        key: value
        for key, value in payload.items()
        if key not in {"content_hash_sha256", "content_hash", "handoff_id", "universe_id"}
    }
    return hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise UniverseHandoffError(f"source artifact is unreadable: {path.name}") from exc
    return digest.hexdigest()


def _date_text(value: Any) -> str | None:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return _date_text(text[:10])


def _first_text(*values: Any) -> str:
    return next((str(value).strip() for value in values if str(value or "").strip()), "")


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or validate a governed PaperOps universe handoff"
    )
    parser.add_argument("--morning-root")
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--out")
    parser.add_argument("--handoff")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.validate:
            payload = validate_universe_handoff(args.handoff or args.out or "", args.market_date)
        else:
            payload = build_universe_handoff(
                args.morning_root or "", args.market_date, output_path=args.out
            )
    except (OSError, TypeError, ValueError, UniverseHandoffError) as exc:
        print(f"status: invalid\ndetail: {exc}")
        return 2
    print(
        "status: valid\n"
        f"handoff_id: {payload['handoff_id']}\n"
        f"universe_count: {len(payload['universe_symbols'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = [
    "SCHEMA_VERSION",
    "UniverseHandoffError",
    "build_universe_handoff",
    "load_universe_handoff",
    "validate_universe_handoff",
]
