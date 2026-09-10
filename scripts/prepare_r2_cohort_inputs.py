"""Bind real mover/capture artifacts into an isolated date-folder adapter.

The adapter writes pointers and derived declarations only. It never copies,
rewrites, or mutates the retained provider receipt, state, pages, or source
census artifacts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

REFERENCE_PANEL = ("DIA", "IWM", "QQQ", "SPY", "TLT")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    return _sha256(path)


def build(
    *,
    handoff: Path,
    snapshot: Path,
    enriched: Path,
    ranked: Path,
    avoid: Path,
    capture_receipt: Path,
    output_root: Path,
    market_date: str,
    seed: str = "dawnstrike-ops02",
    max_candidates: int = 12,
) -> dict[str, Any]:
    handoff_value = json.loads(handoff.read_text(encoding="utf-8"))
    members = handoff_value.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("mover handoff has no members")
    mover_symbols = [str(row.get("symbol") or "").strip().upper() for row in members]
    if len(mover_symbols) != len(set(mover_symbols)) or any(not symbol for symbol in mover_symbols):
        raise ValueError("mover handoff has duplicate or blank symbols")
    snapshot_rows = _rows(snapshot)
    snapshot_symbols = [str(row.get("ticker") or "").strip().upper() for row in snapshot_rows]
    if set(snapshot_symbols) != set(mover_symbols) or len(snapshot_rows) != len(mover_symbols):
        raise ValueError("snapshot is not a complete source-matched mover census")
    enriched_rows = _rows(enriched)
    enriched_by_symbol = {
        str(row.get("ticker") or "").strip().upper(): row for row in enriched_rows
    }
    if set(enriched_by_symbol) != set(mover_symbols):
        raise ValueError("enriched source does not preserve the mover census")
    ranked_symbols = {str(row.get("ticker") or "").strip().upper() for row in _rows(ranked)}
    avoid_symbols = {str(row.get("ticker") or "").strip().upper() for row in _rows(avoid)}
    if ranked_symbols & avoid_symbols:
        raise ValueError("ranked and rejected mover mappings overlap")
    mover_rows: list[dict[str, Any]] = []
    for symbol in mover_symbols:
        enriched_row = enriched_by_symbol[symbol]
        status = str(enriched_row.get("enrichment_status") or "")
        if status == "missing_premarket_bars":
            membership = "missing_input"
            reasons = ["mover_census", "missing_premarket_bars", "unusable_fallback"]
        elif symbol in ranked_symbols:
            membership = "selected"
            reasons = ["mover_census", "ranked_watch"]
        elif symbol in avoid_symbols:
            membership = "rejected"
            reasons = ["mover_census", "avoid_list"]
        else:
            membership = "unselected"
            reasons = ["mover_census", "not_enrichment_selected"]
        mover_rows.append(
            {
                "symbol": symbol,
                "membership": membership,
                "reason_codes": reasons,
                "required_inputs": ["bars", "trades", "quotes", "corporate_actions"],
                "source_lane": "mover",
                "as_of": market_date,
            }
        )
    panel_rows = [
        {
            "symbol": symbol,
            "membership": "selected",
            "reason_codes": ["predeclared_reference_panel"],
            "required_inputs": ["bars", "trades", "quotes", "corporate_actions"],
            "source_lane": "reference_panel",
            "as_of": market_date,
        }
        for symbol in REFERENCE_PANEL
    ]
    source_artifacts = {
        name: {"path": str(path.resolve()), "sha256": _sha256(path)}
        for name, path in {
            "handoff": handoff,
            "snapshot": snapshot,
            "enriched_audit": enriched,
            "ranked": ranked,
            "avoid": avoid,
        }.items()
    }
    scope = {
        "schema_version": "dawnstrike.observation.scope_declaration.v1",
        "market_date": market_date,
        "universe_generation_id": str(handoff_value.get("universe_id") or "") + ":ops02-fresh",
        "scope_policy": {
            "source_scope": "actual date-bound mover census",
            "variable_cardinality": True,
            "small_cap_eligibility": "not_instantiated",
            "core_index_membership": "separate unavailable scope; never inferred here",
            "reference_panel": list(REFERENCE_PANEL),
            "prospective_sampling": "deterministic stratified hash order; no outcomes",
        },
        "producer_completeness": {
            "status": "COMPLETE",
            "source_count": len(mover_rows),
            "declared_count": len(mover_rows),
            "included_count": len(mover_rows),
            "source_as_of": market_date,
            "truncated": False,
            "survivorship_filter": False,
            "padding": False,
        },
        "source_identity": {
            "market_date": market_date,
            "as_of": market_date,
            "handoff_id": handoff_value.get("handoff_id"),
            "source_kind": "fresh_daily_mover_census",
        },
        "source_artifacts": source_artifacts,
        "prospective_sampling": {
            "seed": seed,
            "max_candidates": max_candidates,
            "selection_time": "before any prospective capture or outcome",
            "outcome_independent": True,
        },
        "scopes": {
            "original_small_cap_gap": mover_rows,
            "liquid_reference_panel": panel_rows,
        },
    }
    output_root = output_root.resolve()
    session_root = output_root / market_date
    scope_path = session_root / "scope.json"
    scope_sha = _write(scope_path, scope)
    receipt_value = json.loads(capture_receipt.read_text(encoding="utf-8"))
    if receipt_value.get("market_date") != market_date:
        raise ValueError("retained capture receipt date does not match adapter date")
    state_path = Path(str(receipt_value.get("state_path") or ""))
    if not state_path.is_file():
        raise ValueError("retained capture state is missing")
    pointer = {
        "schema_version": "dawnstrike.observation.capture_source_pointer.v1",
        "market_date": market_date,
        "session_id": receipt_value.get("session_id"),
        "receipt_path": str(capture_receipt.resolve()),
        "receipt_sha256": _sha256(capture_receipt),
        "state_path": str(state_path.resolve()),
        "state_sha256": _sha256(state_path),
        "source_config_sha256": receipt_value.get("source_config_hash"),
        "raw_source_preserved": True,
        "provider_call_performed": False,
    }
    pointer_path = session_root / "capture-source.json"
    pointer_sha = _write(pointer_path, pointer)
    adapter_receipt = {
        "schema_version": "dawnstrike.r2_ops02_input_adapter_receipt.v1",
        "status": "DEVELOPMENT_BOUND",
        "market_date": market_date,
        "scope_path": str(scope_path),
        "scope_sha256": scope_sha,
        "capture_pointer_path": str(pointer_path),
        "capture_pointer_sha256": pointer_sha,
        "mover_source_count": len(mover_rows),
        "reference_panel_count": len(panel_rows),
        "historical_or_development_only": True,
        "prospective_job_created": False,
        "provider_call_performed": False,
    }
    adapter_path = session_root / "adapter-receipt.json"
    adapter_sha = _write(adapter_path, adapter_receipt)
    return {
        "status": "DEVELOPMENT_BOUND",
        "market_date": market_date,
        "mover_source_count": len(mover_rows),
        "scope_path": str(scope_path),
        "scope_sha256": scope_sha,
        "capture_pointer_path": str(pointer_path),
        "capture_pointer_sha256": pointer_sha,
        "adapter_receipt_path": str(adapter_path),
        "adapter_receipt_sha256": adapter_sha,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "handoff",
        "snapshot",
        "enriched",
        "ranked",
        "avoid",
        "capture_receipt",
        "output_root",
    ):
        parser.add_argument(f"--{name.replace('_', '-')}", type=Path, required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--seed", default="dawnstrike-ops02")
    parser.add_argument("--max-candidates", type=int, default=12)
    args = parser.parse_args()
    try:
        print(json.dumps(build(**vars(args)), sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
