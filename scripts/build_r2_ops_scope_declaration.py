"""Build the corrected R2 two-scope declaration from retained mover artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


REFERENCE_PANEL = ("DIA", "IWM", "QQQ", "SPY", "TLT")
SCHEMA = "dawnstrike.observation.scope_declaration.v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build(
    *,
    handoff: Path,
    snapshot: Path,
    enriched: Path,
    ranked: Path,
    avoid: Path,
    output: Path,
    market_date: str,
    seed: str,
    max_candidates: int,
) -> dict[str, Any]:
    handoff_value = json.loads(handoff.read_text(encoding="utf-8"))
    members = handoff_value.get("members")
    if not isinstance(members, list):
        raise ValueError("handoff members are missing")
    mover_symbols = [str(row.get("symbol") or "").strip().upper() for row in members]
    if len(mover_symbols) != 181 or len(set(mover_symbols)) != 181:
        raise ValueError("retained mover handoff must contain 181 unique symbols")
    snapshot_rows = _read_rows(snapshot)
    snapshot_symbols = [str(row.get("ticker") or "").strip().upper() for row in snapshot_rows]
    if snapshot_symbols != mover_symbols and set(snapshot_symbols) != set(mover_symbols):
        raise ValueError("snapshot symbols do not match the mover handoff")
    enriched_rows = _read_rows(enriched)
    enriched_by_symbol = {
        str(row.get("ticker") or "").strip().upper(): row for row in enriched_rows
    }
    if set(enriched_by_symbol) != set(mover_symbols):
        raise ValueError("enriched audit symbols do not match the mover handoff")
    ranked_symbols = {
        str(row.get("ticker") or "").strip().upper() for row in _read_rows(ranked)
    }
    avoid_rows = _read_rows(avoid)
    avoid_symbols = {
        str(row.get("ticker") or "").strip().upper() for row in avoid_rows
    }
    if ranked_symbols & avoid_symbols:
        raise ValueError("ranked and avoid mappings overlap")

    rows: list[dict[str, Any]] = []
    for symbol in mover_symbols:
        enriched_row = enriched_by_symbol[symbol]
        status = enriched_row.get("enrichment_status") or ""
        if status == "missing_premarket_bars":
            membership = "missing_input"
            reason_codes = ["mover_census", "missing_premarket_bars", "unusable_fallback"]
        elif symbol in ranked_symbols:
            membership = "selected"
            reason_codes = ["mover_census", "ranked_watch"]
        elif symbol in avoid_symbols:
            membership = "rejected"
            reason_codes = ["mover_census", "avoid_list"]
        else:
            membership = "unselected"
            reason_codes = ["mover_census", "not_enrichment_selected"]
        rows.append(
            {
                "symbol": symbol,
                "membership": membership,
                "reason_codes": reason_codes,
                "required_inputs": ["bars", "trades", "quotes", "corporate_actions"],
                "source_lane": "mover",
                "as_of": market_date,
            }
        )

    sample = sorted(
        mover_symbols,
        key=lambda symbol: hashlib.sha256(f"{seed}:{symbol}".encode()).hexdigest(),
    )[:max_candidates]
    source_artifacts = {
        "handoff": {"path": str(handoff), "sha256": _sha256(handoff)},
        "snapshot": {"path": str(snapshot), "sha256": _sha256(snapshot)},
        "enriched_audit": {"path": str(enriched), "sha256": _sha256(enriched)},
        "ranked": {"path": str(ranked), "sha256": _sha256(ranked)},
        "avoid": {"path": str(avoid), "sha256": _sha256(avoid)},
    }
    declaration: dict[str, Any] = {
        "schema_version": SCHEMA,
        "universe_generation_id": str(handoff_value.get("universe_id") or "")
        + ":ops-corrected",
        "market_date": market_date,
        "scope_policy": {
            "source_scope": "actual retained 181-row mover census",
            "small_cap_eligibility": "not_instantiated",
            "core_index_membership": "separate unavailable scope; never inferred here",
            "reference_panel": list(REFERENCE_PANEL),
            "prospective_sampling": "predeclared deterministic hash order; no outcomes",
        },
        "prospective_sampling": {
            "seed": seed,
            "max_candidates": max_candidates,
            "selected_count": len(sample),
            "selected_symbols": sample,
            "cohort_sessions_max": 10,
            "selection_time": "before any prospective capture or outcome",
        },
        "source_artifacts": source_artifacts,
        "source_identity": {
            "handoff_id": handoff_value.get("handoff_id"),
            "run_id": handoff_value.get("run_id"),
            "mover_source": handoff_value.get("mover_source"),
            "market_date": market_date,
        },
        "scopes": {
            "original_small_cap_gap": rows,
            "liquid_reference_panel": [
                {
                    "symbol": symbol,
                    "membership": "selected",
                    "reason_codes": ["predeclared_reference_panel"],
                    "required_inputs": ["bars", "trades", "quotes", "corporate_actions"],
                    "source_lane": "reference_panel",
                    "as_of": market_date,
                }
                for symbol in REFERENCE_PANEL
            ],
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(json.dumps(declaration, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    return {
        "path": str(output),
        "sha256": _sha256(output),
        "mover_count": len(rows),
        "reference_count": len(REFERENCE_PANEL),
        "membership_counts": {
            membership: sum(row["membership"] == membership for row in rows)
            for membership in ("selected", "rejected", "unselected", "missing_input")
        },
        "prospective_sample": sample,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--enriched", type=Path, required=True)
    parser.add_argument("--ranked", type=Path, required=True)
    parser.add_argument("--avoid", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--seed", default="dawnstrike-r2-ops-20260910")
    parser.add_argument("--max-candidates", type=int, default=12)
    args = parser.parse_args()
    print(json.dumps(build(**vars(args)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
