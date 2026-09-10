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
    return result, {
        "path": str(source_db.resolve()),
        "sha256": _sha256(source_db),
        "market_date": market_date,
        "row_count": len(result),
        "read_only": True,
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
            "isolated_database": {"path": str(db_path), "sha256": _sha256(db_path)},
        },
        "versioned_universe_id": version["universe_id"],
        "persisted_to_isolated_database": persisted,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt_path = output_root / "registration-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--alpha-cycle", type=Path, required=True)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--entitlement", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--market-date", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(**vars(args)), indent=2, sort_keys=True))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
