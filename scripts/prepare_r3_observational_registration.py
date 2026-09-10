"""Bind actual historical Alpha-cycle decisions to an isolated R3 database.

The source DB is opened read-only.  The output database receives only an
observational universe registration whose external approval state is preserved;
active production state is never opened for writing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.services.alpha_v6_universe_service import (
    prepare_alpha_v6_universe,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _extract_decisions(
    source_db: Path, market_date: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    uri = f"file:{source_db.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM alpha_v6_decisions WHERE market_date = ? "
            "ORDER BY decision_at, decision_id",
            (market_date,),
        ).fetchall()
    if not rows:
        raise ValueError(f"source DB has no alpha_v6_decisions for {market_date}")
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError("source decision payload_json must be an object")
        merged = dict(payload)
        for field in (
            "decision_id", "scan_id", "market_date", "decision_at", "ticker",
            "strategy_version", "model_version", "action", "setup_key",
            "source_lineage_hash_sha256", "source_artifact_hash_sha256",
        ):
            if field in row.keys() and row[field] is not None:
                merged[field] = row[field]
        if str(merged.get("market_date") or "") != market_date:
            raise ValueError("source DB returned a cross-date decision")
        result.append(merged)
    # A live SQLite file can change after this read.  Keep both the file hash
    # (useful evidence) and a canonical row-set identity; the latter is the
    # identity consumers may bind to without pretending the file is immutable.
    rowset_hash = canonical_hash(result)
    snapshot_hash = _sha256(source_db)
    return result, {
        "path": str(source_db.resolve()),
        "sha256": snapshot_hash,
        "whole_file_sha256_at_read": snapshot_hash,
        "rowset_sha256": rowset_hash,
        "market_date": market_date,
        "row_count": len(result),
        "read_only": True,
        "query_only": True,
        "snapshot_semantics": "read_only_rowset_at_extraction_time",
        "whole_file_hash_is_not_a_stable_live_snapshot": True,
    }


def _source_evidence(alpha_cycle: dict[str, Any]) -> dict[str, Any]:
    attempts = list((alpha_cycle.get("source_summary") or {}).get("attempts") or [])
    for attempt in attempts:
        evidence = attempt.get("universe_evidence")
        if isinstance(evidence, dict) and evidence.get("members"):
            return evidence
    raise ValueError("historical alpha-cycle lacks authenticated universe evidence")


def _members(handoff: dict[str, Any], market_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for member in handoff.get("members") or []:
        if not isinstance(member, dict):
            raise ValueError("handoff member is not an object")
        ticker = str(member.get("symbol") or "").strip().upper()
        lineage = member.get("member_lineage") or {}
        source_row = lineage.get("source_row") or {}
        if not ticker:
            raise ValueError("handoff member has no symbol")
        rows.append(
            {
                "ticker": ticker,
                "listing_status": "ACTIVE",
                "valid_from": market_date,
                "valid_to": None,
                "previous_ticker": None,
                "corporate_action_type": None,
                "source_ref": str(lineage.get("source_identity") or ""),
                "eligibility": {
                    "asset_class": "us_equity",
                    "lane": str(member.get("lane") or "mover"),
                    "source_status": str(lineage.get("source_status") or "unknown"),
                    "corporate_action_status": str(
                        source_row.get("corporate_action_status") or "UNKNOWN"
                    ),
                },
            }
        )
    if len(rows) != int((handoff.get("mover_source") or {}).get("declared_count") or 0):
        raise ValueError("handoff member count does not match declared source count")
    if len({row["ticker"] for row in rows}) != len(rows):
        raise ValueError("handoff contains duplicate tickers")
    return rows


def prepare(
    *,
    source_db: Path,
    alpha_cycle: Path,
    handoff: Path,
    snapshot: Path,
    entitlement: Path,
    output_root: Path,
    market_date: str,
    persist_database: bool = True,
) -> dict[str, Any]:
    alpha = _read_json(alpha_cycle, "alpha cycle")
    handoff_value = _read_json(handoff, "universe handoff")
    entitlement_value = _read_json(entitlement, "entitlement receipt")
    if alpha.get("scan_id") != handoff_value.get("run_id"):
        raise ValueError("alpha-cycle and handoff run identities do not match")
    if handoff_value.get("market_date") != market_date:
        raise ValueError("handoff market date does not match requested date")
    decisions, decision_db_identity = _extract_decisions(source_db, market_date)
    evidence = _source_evidence(alpha)
    members = _members(handoff_value, market_date)
    registration_approved = evidence.get("registration_approved") is True
    approval_status = "APPROVED" if registration_approved else "PENDING_EXTERNAL_APPROVAL"
    contract = {
        "schema_version": "dawnstrike.alphaops_v6.universe_source_contract.v1",
        "source_id": "alpaca:stocks-screener-plus-active-assets",
        "provider_id": str(evidence.get("provider_id") or "alpaca"),
        "dataset_id": str(evidence.get("dataset_id") or "stocks-screener-plus-active-assets"),
        "dataset_version": str(evidence.get("dataset_version") or evidence.get("retrieved_at")),
        "terms_reference": str(evidence.get("terms_reference") or "https://docs.alpaca.markets/"),
        "entitlement_reference": str(
            evidence.get("entitlement_reference") or "configured-alpaca-account"
        ),
        "accountable_contact": str(
            evidence.get("accountable_contact") or "dawnstrikebot@gmail.com"
        ),
        "approval_status": approval_status,
        "critical_truth_complete": False,
        "registration_allowed": registration_approved,
        "expected_artifact_sha256": _sha256(handoff),
    }
    contract_hash = canonical_hash(contract)
    source_lineage = {
        **contract,
        "source_contract_hash_sha256": contract_hash,
        "retrieved_at": str(evidence.get("retrieved_at") or datetime.now(UTC).isoformat()),
        "raw_artifact_sha256": _sha256(handoff),
        "configuration_hash_sha256": canonical_hash(
            {"market_date": market_date, "members": members, "source": contract_hash}
        ),
    }
    version, normalized = prepare_alpha_v6_universe(
        as_of_date=market_date,
        members=members,
        source_lineage=source_lineage,
        require_registration_approval=False,
    )
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    db_path = output_root / "observational-registration.sqlite"
    persisted = False
    if persist_database:
        persisted = SQLiteScanStore(db_path).persist_alpha_v6_universe(
            version=version, members=normalized
        )
    decisions_artifact = {
        "schema_version": "dawnstrike.alphaops_v6.actual_decision_extraction.v1",
        "source_kind": "read_only_shadow_real.sqlite",
        "market_date": market_date,
        "source_db": decision_db_identity,
        "source_alpha_cycle": {"path": str(alpha_cycle.resolve()), "sha256": _sha256(alpha_cycle)},
        "v6_decision_records": decisions,
    }
    decisions_path = output_root / "alpha_v6_decisions.actual.json"
    decisions_path.write_text(
        json.dumps(decisions_artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    contract_path = output_root / "universe-source-contract.json"
    contract_path.write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    universe_path = output_root / "observational-universe.json"
    universe_path.write_text(
        json.dumps({"version": version, "members": normalized}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt = {
        "schema_version": "dawnstrike.r3.observational_registration.v1",
        "status": "OBSERVATIONAL_REGISTERED_PENDING_EXTERNAL_APPROVAL",
        "market_date": market_date,
        "decision_count": len(decisions),
        "universe_member_count": len(normalized),
        "registration_approved_in_source": registration_approved,
        "production_registration_performed": False,
        "source": {
            "alpha_cycle": {"path": str(alpha_cycle.resolve()), "sha256": _sha256(alpha_cycle)},
            "handoff": {"path": str(handoff.resolve()), "sha256": _sha256(handoff)},
            "premarket_snapshot": {"path": str(snapshot.resolve()), "sha256": _sha256(snapshot)},
            "entitlement": {
                "path": str(entitlement.resolve()),
                "sha256": _sha256(entitlement),
                "approved_plan": entitlement_value.get("approved_plan") is True,
                "broker_execution": entitlement_value.get("broker_execution"),
                "feed": entitlement_value.get("feed"),
            },
            "decision_db": decision_db_identity,
        },
        "artifacts": {
            "decision_artifact": {"path": str(decisions_path), "sha256": _sha256(decisions_path)},
            "source_contract": {"path": str(contract_path), "sha256": _sha256(contract_path)},
            "universe": {"path": str(universe_path), "sha256": _sha256(universe_path)},
            "isolated_database": (
                {"path": str(db_path), "sha256": _sha256(db_path)}
                if persist_database else None
            ),
        },
        "versioned_universe_id": version["universe_id"],
        "persisted_to_isolated_database": persisted,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt_path = output_root / "registration-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def discover_actual_inputs(*, source_root: Path, market_date: str,
                           entitlement: Path | None = None,
                           source_config: Path | None = None) -> dict[str, Path]:
    """Resolve the date-bound, existing source inputs without provider access."""
    root = source_root.resolve()
    date_root = root / "outputs" / "alpha_cycle" / market_date
    alpha = date_root / "alpha_cycle.json"
    handoff = date_root / "paperops_universe_handoff.json"
    snapshot = date_root / "web_collect" / "premarket_snapshot.csv"
    source_db = root / "shadow_real.sqlite"
    entitlement = entitlement.resolve() if entitlement else (
        root / "receipts" / f"paper-execution-{market_date}.json"
    )
    values = {
        "alpha_cycle": alpha, "handoff": handoff, "snapshot": snapshot,
        "source_db": source_db, "entitlement": entitlement,
    }
    candidate_config = (
        source_config.resolve()
        if source_config else root / "config" / "web_sources.yaml"
    )
    # Configuration is part of the authenticated source contract.  Keep the
    # missing path in the required-input set so actual mode cannot silently
    # register a producer without provider/feed semantics.
    values["source_config"] = candidate_config
    missing = [name for name, path in values.items() if not path.is_file()]
    if missing:
        raise ValueError("actual source inputs missing: " + ", ".join(missing))
    alpha_value = _read_json(alpha, "alpha cycle")
    handoff_value = _read_json(handoff, "universe handoff")
    if str(handoff_value.get("market_date")) != market_date:
        raise ValueError("actual handoff is for a different market date")
    if str(alpha_value.get("scan_id")) != str(handoff_value.get("run_id")):
        raise ValueError("actual alpha-cycle and handoff run identities differ")
    snapshot_bytes = snapshot.read_bytes()
    header = snapshot_bytes.splitlines()[0].lower() if snapshot_bytes.splitlines() else b""
    if not snapshot_bytes.strip() or (b"ticker" not in header and b"symbol" not in header):
        raise ValueError("actual premarket snapshot is malformed")
    try:
        entitlement_value = _read_json(entitlement, "entitlement receipt")
    except ValueError:
        raise
    if str(entitlement_value.get("market_date") or market_date) != market_date:
        raise ValueError("actual entitlement receipt date mismatch")
    return values


def prepare_actual_observational_registration(
    *, source_root: Path, output_root: Path, market_date: str,
    entitlement: Path | None = None, source_config: Path | None = None,
    typed_census: Path | None = None,
) -> dict[str, Any]:
    """Create an authenticated, bounded date-local producer bundle.

    This is source discovery and registration only.  It never opens the
    active database for writes and deliberately leaves the isolated universe
    in the caller's bounded/in-memory registration store.
    """
    paths = discover_actual_inputs(
        source_root=source_root,
        market_date=market_date,
        entitlement=entitlement,
        source_config=source_config,
    )
    receipt = prepare(
        source_db=paths["source_db"], alpha_cycle=paths["alpha_cycle"],
        handoff=paths["handoff"], snapshot=paths["snapshot"],
        entitlement=paths["entitlement"], output_root=output_root,
        market_date=market_date, persist_database=False,
    )
    alpha = _read_json(paths["alpha_cycle"], "alpha cycle")
    handoff = _read_json(paths["handoff"], "universe handoff")
    decisions, db_identity = _extract_decisions(paths["source_db"], market_date)
    decision_by_symbol = {
        str(row.get("ticker") or "").upper(): row for row in decisions
    }
    members = _members(handoff, market_date)
    # Membership is derived from the source decision disposition only for
    # sampling metadata; the original decision action remains untouched.
    mover_rows: list[dict[str, Any]] = []
    for member in members:
        ticker = str(member["ticker"]).upper()
        decision = decision_by_symbol.get(ticker)
        action = str((decision or {}).get("action") or "").upper()
        if action.startswith("SHADOW_REJECT"):
            membership = "rejected"
        elif action in {"SHADOW_SELECTED", "SELECTED", "BUY", "ENTER"}:
            membership = "selected"
        elif action in {"SHADOW_NO_TRADE", "SHADOW_BLOCKED", "BLOCKED"}:
            membership = "missing_input"
        else:
            membership = "unselected"
        mover_rows.append({
            "symbol": ticker, "membership": membership, "source_lane": "mover",
            "source_ref": member.get("source_ref"),
            "required_inputs": ["bars"],
            "decision_id": (decision or {}).get("decision_id"),
            "decision_action": action or None,
        })
    if typed_census is not None:
        census_path = typed_census.resolve()
        if not census_path.is_file():
            raise ValueError("authenticated typed census is missing")
        census_value = json.loads(census_path.read_text(encoding="utf-8"))
        if (
            not isinstance(census_value, list)
            or not all(isinstance(row, dict) for row in census_value)
        ):
            raise ValueError("authenticated typed census is malformed")
        census_by_symbol = {
            str(row.get("symbol") or "").upper(): str(row.get("membership") or "")
            for row in census_value
        }
        if set(census_by_symbol) != {str(row["symbol"]).upper() for row in mover_rows}:
            raise ValueError("authenticated typed census does not match actual handoff members")
        if any(
            value not in {"selected", "rejected", "unselected", "missing_input"}
            for value in census_by_symbol.values()
        ):
            raise ValueError("authenticated typed census has an invalid membership")
        for row in mover_rows:
            row["membership"] = census_by_symbol[str(row["symbol"]).upper()]
    panel = [
        {"symbol": symbol, "membership": "selected", "source_lane": "reference_panel",
         "required_inputs": ["bars"]}
        for symbol in ("DIA", "IWM", "QQQ", "SPY", "TLT")
    ]
    source_artifacts = {}
    for name, path in paths.items():
        source_artifacts[name] = {"path": str(path), "sha256": _sha256(path)}
        if name == "source_db":
            source_artifacts[name].update({
                "hash_semantics": "read_only_rowset_at_extraction_time",
                "rowset_sha256": db_identity["rowset_sha256"],
                "whole_file_hash_is_not_a_stable_live_snapshot": True,
            })
    if typed_census is not None:
        source_artifacts["typed_census"] = {
            "path": str(typed_census.resolve()), "sha256": _sha256(typed_census),
            "hash_semantics": "authenticated_full_census_membership",
        }
    source_identity = {
        "market_date": market_date,
        "scan_id": alpha.get("scan_id"),
        "handoff_run_id": handoff.get("run_id"),
        "handoff_universe_id": handoff.get("universe_id"),
        "handoff_id": handoff.get("handoff_id"),
        "source_status": (handoff.get("mover_source") or {}).get("status"),
        "source_identity": (handoff.get("mover_source") or {}).get("source_identity"),
        "decision_rowset_sha256": db_identity["rowset_sha256"],
        "typed_census_sha256": _sha256(typed_census) if typed_census is not None else None,
    }
    scope = {
        "schema_version": "dawnstrike.observation.scope_declaration.v1",
        "market_date": market_date,
        "source_identity": source_identity,
        "source_artifacts": source_artifacts,
        "producer_completeness": {
            "status": "COMPLETE",
            "source_count": len(mover_rows), "declared_count": len(mover_rows),
            "included_count": len(mover_rows), "truncated": False,
            "survivorship_filter": False, "source_as_of": market_date,
            "full_census": True, "missing_input_count": sum(
                row["membership"] == "missing_input" for row in mover_rows
            ),
        },
        "missing_input": False,
        "scopes": {"original_small_cap_gap": mover_rows, "liquid_reference_panel": panel},
        "registration": {
            "receipt_sha256": _sha256(output_root.resolve() / "registration-receipt.json"),
            "versioned_universe_id": receipt["versioned_universe_id"],
            "external_approval": False,
            "production_registration_performed": False,
            "research_only": True,
        },
        "decision_artifact": {
            "path": str((output_root / "alpha_v6_decisions.actual.json").resolve()),
            "sha256": _sha256(output_root / "alpha_v6_decisions.actual.json"),
            "decision_count": len(decisions),
        },
    }
    output_root = output_root.resolve()
    scope_path = output_root / "scope.json"
    scope_path.write_text(json.dumps(scope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt["source"]["decision_db"] = db_identity
    receipt["source"]["snapshot_semantics"] = db_identity["snapshot_semantics"]
    receipt["source"]["source_identity"] = source_identity
    if typed_census is not None:
        receipt["source"]["typed_census"] = {
            "path": str(typed_census.resolve()), "sha256": _sha256(typed_census),
            "membership_counts": {
                name: sum(row["membership"] == name for row in mover_rows)
                for name in ("selected", "rejected", "unselected", "missing_input")
            },
        }
    # The scope binds the receipt hash below.  Keep only the path in the
    # receipt to avoid a circular receipt<->scope hash relationship.
    receipt["scope"] = {"path": str(scope_path)}
    receipt_path = output_root / "registration-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Rewrite scope's registration hash now that receipt contains its final
    # source identity; hashes are content-bound and deterministic thereafter.
    scope["registration"]["receipt_sha256"] = _sha256(receipt_path)
    scope_path.write_text(json.dumps(scope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "READY", "paths": {k: str(v) for k, v in paths.items()},
            "receipt": receipt, "scope": scope, "scope_path": str(scope_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--alpha-cycle", type=Path)
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--entitlement", type=Path)
    parser.add_argument("--actual-source-root", type=Path,
                        help="discover the date-bound existing state inputs")
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--actual-census", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--market-date", required=True)
    args = parser.parse_args()
    try:
        if args.actual_source_root is not None:
            if args.output_root is None or args.market_date is None:
                raise ValueError("actual mode requires --output-root and --market-date")
            value = prepare_actual_observational_registration(
                source_root=args.actual_source_root, output_root=args.output_root,
                market_date=args.market_date, entitlement=args.entitlement,
                source_config=args.source_config,
                typed_census=args.actual_census,
            )
        else:
            required = (args.source_db, args.alpha_cycle, args.handoff, args.snapshot,
                        args.entitlement, args.output_root, args.market_date)
            if any(value is None for value in required):
                raise ValueError("explicit mode requires all source paths and market date")
            value = prepare(
                source_db=args.source_db, alpha_cycle=args.alpha_cycle,
                handoff=args.handoff, snapshot=args.snapshot,
                entitlement=args.entitlement, output_root=args.output_root,
                market_date=args.market_date,
            )
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
