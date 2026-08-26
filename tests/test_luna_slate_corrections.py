from __future__ import annotations

import hashlib
import json
from pathlib import Path

from intraday_scanner.services import luna_core_universe_service as core
from intraday_scanner.services.luna_core_universe_service import (
    build_core_universe_contract,
    discover_core_universe_rows,
)
from intraday_scanner.services.luna_research_slate_service import (
    build_ranked_research_slate,
    persist_ranked_research_slate,
    validate_ranked_research_slate,
)


def _manifest(index: str, symbol: str, *, observed: str = "2026-08-26T12:00:00Z") -> dict:
    record = {
        "ticker": symbol,
        "provider_symbol": symbol,
        "asset_class": "common_stock",
        "index_memberships": [index],
        "valid_from": "2026-01-01",
    }
    return {
        "source_id": index,
        "source_uri": f"https://provider.invalid/{symbol}",
        "observed_at": observed,
        "effective_date": "2026-01-01",
        "reconstitution_id": "rebalance-2026-01",
        "members": [record],
        "index_name": index,
        "expected_count": 1,
        "test_override": True,
        "canonical_member_set_hash_sha256": core._canonical_member_hash(
            [{**record, "index": index}]
        ),
    }


def test_wrapper_expands_each_source_and_resolves_relative_raw_artifacts(tmp_path: Path) -> None:
    raw = tmp_path / "raw.txt"
    raw.write_text("source-bytes", encoding="utf-8")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    children = []
    for index, symbol in (("S&P 500", "SP"), ("Nasdaq-100", "NQ")):
        child = _manifest(index, symbol)
        child["test_override"] = True
        child["source_artifacts"] = [
            {"uri": "https://raw.invalid", "path": "raw.txt", "sha256": digest}
        ]
        children.append(child)
    wrapper = tmp_path / "manifest.json"
    wrapper.write_text(json.dumps({"manifests": children}), encoding="utf-8")

    contract = build_core_universe_contract(
        wrapper,
        observed_at="2026-08-26T13:00:00Z",
        market_date="2026-08-26",
        allow_test_override=True,
    )

    assert contract["status"] == "READY"
    assert len(contract["source_artifacts"]) == 2
    assert contract["raw_artifact_hashes"] == [digest, digest]


def test_future_observation_and_tiny_production_manifest_are_unavailable() -> None:
    manifest = _manifest("S&P 500", "SP", observed="2026-08-27T12:00:00Z")
    manifest.pop("test_override")
    contract = build_core_universe_contract(
        manifest,
        observed_at="2026-08-26T13:00:00Z",
        market_date="2026-08-26",
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert "future_observed_at" in contract["blockers"]
    assert "expected_count_below_production_minimum:S&P 500" in contract["blockers"]


def test_self_declared_test_and_hash_without_bytes_cannot_bypass_production() -> None:
    manifest = _manifest("S&P 500", "SP")
    manifest["raw_artifact_sha256"] = "a" * 64
    manifest["test_override"] = True
    manifest.pop("canonical_member_set_hash_sha256")
    contract = build_core_universe_contract(
        manifest,
        observed_at="2026-08-26T13:00:00Z",
        market_date="2026-08-26",
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert "raw_artifact_bytes_missing" in contract["blockers"]
    assert "expected_count_below_production_minimum:S&P 500" in contract["blockers"]


def test_discovery_partial_batch_is_explicitly_incomplete() -> None:
    class Provider:
        def get_premarket_snapshot(self, symbols, config):
            return [{"ticker": symbols[0], "premarket_price": 10, "premarket_volume": 100}]

    result = discover_core_universe_rows(
        {
            "status": "READY",
            "members": [
                {"symbol": "A", "index_memberships": ["S&P 500"]},
                {"symbol": "B", "index_memberships": ["S&P 500"]},
            ],
        },
        config=object(),
        provider=Provider(),
        batch_size=2,
    )
    assert result["status"] == "INCOMPLETE"
    assert result["coverage_receipts"][0]["missing_symbols"] == ["B"]


def test_slate_has_immutable_identity_and_persistence(tmp_path: Path) -> None:
    slate = build_ranked_research_slate(
        [{"ticker": "AAA", "signal_id": "signal-1"}],
        generated_at="2026-08-26T13:00:00Z",
        market_date="2026-08-26",
        scan_id="scan-1",
    )
    path = persist_ranked_research_slate(slate, tmp_path / "slate.json")
    assert slate["slate_id"].startswith("luna-slate-")
    assert slate["content_hash_sha256"]
    assert path.exists()
    original = path.read_text(encoding="utf-8")
    replacement = build_ranked_research_slate(
        [{"ticker": "BBB", "signal_id": "signal-2"}],
        generated_at="2026-08-26T14:00:00Z",
        market_date="2026-08-26",
        scan_id="scan-2",
    )
    persist_ranked_research_slate(replacement, path)
    assert path.read_text(encoding="utf-8") == original
    slate["symbols"] = ["TAMPERED"]
    try:
        validate_ranked_research_slate(slate, market_date="2026-08-26")
    except ValueError:
        pass
    else:
        raise AssertionError("tampered slate must fail integrity validation")


def test_tier_one_requires_positive_current_clear_safety_evidence() -> None:
    safe = {
        "ticker": "SAFE",
        "source_count": 1,
        "freshness_status": "FRESH",
        "halt_status": "CLEAR",
        "sec_risk_status": "CLEAR",
        "corporate_action_status": "CLEAR",
        "input_status": "VERIFIED",
        "evidence_status": "VERIFIED",
    }
    unknown = {**safe, "ticker": "UNKNOWN", "sec_risk_status": "UNKNOWN"}
    slate = build_ranked_research_slate([unknown, safe], target=5, require_safety=True)
    assert slate["symbols"] == ["SAFE"]
    assert "sec_risk_status_not_clear" in slate["safety_blockers"]
