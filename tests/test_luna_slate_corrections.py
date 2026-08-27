from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from intraday_scanner.services import luna_core_universe_service as core
from intraday_scanner.services.alpha_cycle_service import _merge_lane_candidates
from intraday_scanner.services.luna_core_universe_service import (
    build_core_universe_contract,
    discover_core_universe_rows,
)
from intraday_scanner.services.luna_research_slate_service import (
    TIER1,
    TIER2,
    apply_publication_semantics,
    build_ranked_research_slate,
    persist_ranked_research_slate,
    publication_counts,
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


def test_production_core_contract_replays_spy_bytes_before_accepting_recomputed_member_hash(
    tmp_path: Path, monkeypatch
) -> None:
    """Changing the declared member and recomputing its hash cannot forge a READY core."""

    symbols = [f"AA{index:03d}" for index in range(503)]
    rows = [
        '<row r="3"><c r="B3" t="inlineStr"><is><t>As of 24-Aug-2026</t></is></c></row>',
        '<row r="5"><c r="B5" t="inlineStr"><is><t>Ticker</t></is></c></row>',
    ]
    for number, symbol in enumerate([*symbols, "-", "2602335D"], start=6):
        rows.append(
            f'<row r="{number}"><c r="A{number}" t="inlineStr"><is><t>Security</t></is></c>'
            f'<c r="B{number}" t="inlineStr"><is><t>{symbol}</t></is></c></row>'
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(rows)}</sheetData></worksheet>'
    ).encode()
    shared = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
    )
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    raw = payload.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    source_id = "test-spy-source-binding"
    monkeypatch.setitem(
        core._TRUSTED_SOURCE_ROOTS,
        source_id,
        {
            "index": "S&P 500",
            "effective_date": "2026-08-24",
            "raw_artifact_hashes": (digest,),
            "transformation_id": "state-street-spy-holdings-parser-v1",
            "lineage_builder_id": "state-street-spy-holdings-parser-v1",
            "lineage_transformation_id": "exclude-cash-and-contra-holdings-v1",
            "reconstitution_id": "spy-holdings-2026-08-24",
            "membership_authority": "tracker_holdings_proxy",
            "official_index_authority": False,
        },
    )
    declared = [
        {
            "symbol": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index": "S&P 500",
            "valid_from": "2026-08-24",
            "valid_to": None,
        }
        for symbol in symbols
    ]
    declared[0]["symbol"] = "FAKE"
    declared[0]["provider_symbol"] = "FAKE"
    member_hash = core._canonical_member_hash(declared)
    path = tmp_path / "spy.xlsx"
    path.write_bytes(raw)
    manifest = {
        "source_id": source_id,
        "source_uri": "https://example.test/spy.xlsx",
        "observed_at": "2026-08-26T12:00:00Z",
        "effective_date": "2026-08-24",
        "index_name": "S&P 500",
        "expected_count": 503,
        "completeness_verdict": "COMPLETE",
        "members": [
            {
                "ticker": row["symbol"],
                "provider_symbol": row["provider_symbol"],
                "asset_class": row["asset_class"],
                "index_memberships": ["S&P 500"],
                "valid_from": row["valid_from"],
            }
            for row in declared
        ],
        "canonical_member_set_hash_sha256": member_hash,
        "source_artifacts": [{"path": str(path), "sha256": digest}],
        "reconstitution_lineage": {
            "schema_version": "dawnstrike.core_universe_lineage.v1",
            "builder_id": "state-street-spy-holdings-parser-v1",
            "transformation_id": "exclude-cash-and-contra-holdings-v1",
            "reconstitution_id": "spy-holdings-2026-08-24",
            "effective_date": "2026-08-24",
            "input_artifact_hashes": [digest],
            "canonical_member_set_hash_sha256": member_hash,
        },
    }
    contract = build_core_universe_contract(
        manifest,
        observed_at="2026-08-26T13:00:00Z",
        market_date="2026-08-26",
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert "source_binding_membership_mismatch" in contract["blockers"]


def test_reconstitution_lineage_requires_order_and_structured_identity() -> None:
    hashes = ["a" * 64, "b" * 64]
    member_hash = "c" * 64
    manifest = {
        "reconstitution_lineage": {
            "schema_version": "dawnstrike.core_universe_lineage.v1",
            "builder_id": "official-source-builder",
            "transformation_id": "ordered-reconstitution",
            "reconstitution_id": "rebalance-2026-08-26",
            "effective_date": "2026-08-26",
            "input_artifact_hashes": hashes,
            "canonical_member_set_hash_sha256": member_hash,
        }
    }
    assert core._has_reconstitution_lineage(
        manifest,
        effective_date="2026-08-26",
        artifact_hashes=hashes,
        member_hash=member_hash,
    )
    reversed_manifest = json.loads(json.dumps(manifest))
    reversed_manifest["reconstitution_lineage"]["input_artifact_hashes"] = list(
        reversed(hashes)
    )
    assert not core._has_reconstitution_lineage(
        reversed_manifest,
        effective_date="2026-08-26",
        artifact_hashes=hashes,
        member_hash=member_hash,
    )
    missing_identity = json.loads(json.dumps(manifest))
    missing_identity["reconstitution_lineage"].pop("transformation_id")
    assert not core._has_reconstitution_lineage(
        missing_identity,
        effective_date="2026-08-26",
        artifact_hashes=hashes,
        member_hash=member_hash,
    )


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


def test_discovery_freshness_is_bound_to_explicit_cycle_observation_time() -> None:
    class Provider:
        def validate_credentials(self):
            return None

        def get_premarket_snapshot(self, symbols, config):
            return [
                {
                    "ticker": symbols[0],
                    "source": "alpaca_iex",
                    "source_timestamp": "2026-01-05T13:00:00+00:00",
                    "premarket_price": 10,
                }
            ]

    result = discover_core_universe_rows(
        {
            "status": "READY",
            "members": [{"symbol": "A", "index_memberships": ["S&P 500"]}],
        },
        config=SimpleNamespace(premarket_enrichment_max_age_seconds=600),
        provider=Provider(),
        observed_at=datetime(2026, 1, 5, 13, 5, tzinfo=timezone.utc),
    )

    assert result["status"] == "READY"
    assert result["rows"][0]["freshness_status"] == "FRESH"
    assert result["rows"][0].get("stale_data_flag") is not True


def test_discovery_does_not_claim_ready_for_stale_or_unverified_batch_rows() -> None:
    class Provider:
        def validate_credentials(self):
            return None

        def get_premarket_snapshot(self, symbols, config):
            return [
                {
                    "ticker": symbols[0],
                    "source": "yahoo",
                    "source_timestamp": "2026-01-01T13:00:00+00:00",
                    "premarket_price": 10,
                }
            ]

    result = discover_core_universe_rows(
        {
            "status": "READY",
            "members": [{"symbol": "A", "index_memberships": ["S&P 500"]}],
        },
        config=SimpleNamespace(premarket_enrichment_max_age_seconds=600),
        provider=Provider(),
        observed_at=datetime(2026, 1, 5, 13, 5, tzinfo=timezone.utc),
    )

    assert result["status"] == "DATA_UNAVAILABLE"
    receipt = result["coverage_receipts"][0]
    assert receipt["provider"] == ""
    assert receipt["observed_at"] == "2026-01-05T13:05:00+00:00"
    assert receipt["max_age_seconds"] == 600
    assert receipt["row_quality"] == [
        {
            "ticker": "A",
            "provider": "",
            "source_verified": False,
            "freshness_status": "STALE",
        }
    ]


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


def test_slate_persistence_reuses_valid_concurrent_winner(tmp_path: Path) -> None:
    path = tmp_path / "slate.json"
    winner = build_ranked_research_slate(
        [{"ticker": "AAA", "signal_id": "signal-1"}],
        generated_at="2026-08-26T13:00:00Z",
        market_date="2026-08-26",
        scan_id="scan-1",
    )
    loser = build_ranked_research_slate(
        [{"ticker": "BBB", "signal_id": "signal-2"}],
        generated_at="2026-08-26T14:00:00Z",
        market_date="2026-08-26",
        scan_id="scan-2",
    )
    persist_ranked_research_slate(winner, path)
    original = path.read_bytes()

    # The loser must consume the validated frozen winner, leaving its bytes
    # and inode untouched rather than replacing it with its own candidate.
    persist_ranked_research_slate(loser, path)
    assert path.read_bytes() == original
    assert json.loads(path.read_text(encoding="utf-8"))["slate_id"] == winner["slate_id"]


def test_slate_persistence_prepublication_failure_leaves_no_partial_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "slate.json"
    slate = build_ranked_research_slate(
        [{"ticker": "AAA", "signal_id": "signal-1"}],
        generated_at="2026-08-26T13:00:00Z",
        market_date="2026-08-26",
        scan_id="scan-1",
    )

    def crash_before_publication(*_args, **_kwargs):
        raise RuntimeError("injected pre-publication crash")

    monkeypatch.setattr(os, "link", crash_before_publication)
    try:
        persist_ranked_research_slate(slate, path)
    except RuntimeError as exc:
        assert str(exc) == "injected pre-publication crash"
    else:
        raise AssertionError("injected publication crash must propagate")

    assert not path.exists()
    assert list(tmp_path.glob(".slate.json.*.tmp")) == []


def test_slate_persistence_rejects_existing_invalid_artifact(tmp_path: Path) -> None:
    path = tmp_path / "slate.json"
    path.write_text("{\"schema_version\": \"broken\"}\n", encoding="utf-8")
    slate = build_ranked_research_slate(
        [{"ticker": "AAA", "signal_id": "signal-1"}],
        generated_at="2026-08-26T13:00:00Z",
        market_date="2026-08-26",
        scan_id="scan-1",
    )

    try:
        persist_ranked_research_slate(slate, path)
    except ValueError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("invalid existing slate must fail closed")

    assert path.read_text(encoding="utf-8") == '{"schema_version": "broken"}\n'


def test_lane_local_fallback_ceiling_does_not_demote_independent_core(
    monkeypatch,
) -> None:
    from intraday_scanner.services import luna_research_slate_service as slate_service

    rows = [
        {"ticker": "MOVE", "universe_lane": "mover", "evidence_lane": "mover"},
        {"ticker": "CORE", "universe_lane": "core", "evidence_lane": "core"},
    ]
    slate = build_ranked_research_slate(
        rows,
        generated_at="2026-08-26T13:00:00+00:00",
        market_date="2026-08-26",
        scan_id="scan-lanes",
    )
    monkeypatch.setattr(slate_service, "_plan_qualified", lambda row, **_: True)
    monkeypatch.setattr(
        slate_service,
        "_alertable",
        lambda row, *, require_watcher_proof: False,
    )
    published = apply_publication_semantics(
        rows,
        slate=slate,
        coverage={
            "lanes": {
                "mover": {"promotion_limited": True},
                "core": {"promotion_limited": False},
            }
        },
    )

    by_ticker = {row["ticker"]: row for row in published}
    assert by_ticker["MOVE"]["publication_tier"] == TIER1
    assert by_ticker["CORE"]["publication_tier"] == TIER2


def test_publication_does_not_graft_frozen_identity_onto_new_same_ticker_signal():
    frozen = {"ticker": "SAME", "signal_id": "signal-frozen", "alpha_score": 10}
    slate = build_ranked_research_slate(
        [frozen],
        generated_at="2026-08-27T13:00:00+00:00",
        market_date="2026-08-27",
        scan_id="scan-frozen",
    )
    replacement = {"ticker": "SAME", "signal_id": "signal-new", "alpha_score": 99}

    published = apply_publication_semantics([replacement], slate=slate)[0]

    assert published["publication_tier"] is None
    assert "research_selection_id" not in published
    assert published["entry_state"] == "NOT_PUBLISHED"


def test_slate_selection_requires_the_declared_evidence_lane_to_be_eligible() -> None:
    rows = [
        {"ticker": "CORE", "universe_lane": "core", "evidence_lane": "core"},
        {"ticker": "MOVE", "universe_lane": "mover", "evidence_lane": "mover"},
        {"ticker": "OVER", "universe_lane": "mover+core", "evidence_lane": ""},
    ]
    slate = build_ranked_research_slate(
        rows,
        target=5,
        generated_at="2026-08-26T13:00:00+00:00",
        market_date="2026-08-26",
        scan_id="scan-lane-eligibility",
        lane_statuses={
            "mover": {"data_eligible": True},
            "core": {"data_eligible": False},
        },
    )

    assert slate["symbols"] == ["MOVE"]
    assert slate["slate_shortfall_reason"]
    validate_ranked_research_slate(slate, market_date="2026-08-26")


def test_overlap_candidate_retains_the_core_row_as_its_evidence_lane() -> None:
    merged = _merge_lane_candidates(
        [
            {
                "ticker": "OVER",
                "score": 10,
                "source": "mover-fallback",
                "discovery_context": "mover",
            }
        ],
        [
            {
                "ticker": "OVER",
                "score": 9,
                "source": "core-authenticated",
                "discovery_context": "S&P 500",
                "core_universe_memberships": "S&P 500",
            }
        ],
    )

    assert len(merged) == 1
    assert merged[0]["universe_lane"] == "mover+core"
    assert merged[0]["evidence_lane"] == "core"
    assert merged[0]["source"] == "core-authenticated"


def test_slate_scan_identity_is_nonempty_and_content_bound() -> None:
    slate = build_ranked_research_slate(
        [{"ticker": "AAA"}],
        generated_at="2026-08-26T13:00:00+00:00",
        market_date="2026-08-26",
    )
    assert slate["scan_id"] == "luna-research:2026-08-26"
    validate_ranked_research_slate(slate, market_date="2026-08-26")


def test_selection_identity_namespaces_duplicate_upstream_ids_by_ticker() -> None:
    slate = build_ranked_research_slate(
        [
            {"ticker": "AAA", "signal_id": "shared-id"},
            {"ticker": "BBB", "signal_id": "shared-id"},
        ],
        generated_at="2026-08-26T13:00:00+00:00",
        market_date="2026-08-26",
        scan_id="scan-shared-id",
    )
    assert len(set(slate["selection_ids"])) == 2
    assert {row["research_source_signal_id"] for row in slate["rows"]} == {"shared-id"}
    validate_ranked_research_slate(slate, market_date="2026-08-26")


def test_publication_excludes_broker_enabled_input_and_forces_disabled_output() -> None:
    enabled = {
        "ticker": "LIVE",
        "broker_execution_enabled": True,
        "broker_execution": "live",
    }
    safe = {"ticker": "SAFE"}
    slate = build_ranked_research_slate(
        [enabled, safe],
        generated_at="2026-08-26T13:00:00+00:00",
        market_date="2026-08-26",
        scan_id="scan-broker-boundary",
    )
    assert slate["symbols"] == ["SAFE"]
    published = apply_publication_semantics([enabled, safe], slate=slate)
    safe_row = next(row for row in published if row["ticker"] == "SAFE")
    assert safe_row["research_only"] is True
    assert safe_row["broker_execution"] == "disabled"
    assert safe_row["broker_execution_enabled"] is False


def test_publication_cannot_inflate_counts_with_unsafe_or_nonselected_tiers() -> None:
    frozen = {"ticker": "FROZEN", "source_count": 1}
    slate = build_ranked_research_slate(
        [frozen],
        generated_at="2026-08-26T13:00:00+00:00",
        market_date="2026-08-26",
        scan_id="scan-hostile-publication",
    )
    hostile_selected = {
        **frozen,
        "publication_tier": "ALERTABLE_PAPER_ENTRY",
        "research_only": False,
        "broker_execution": "live",
        "broker_execution_enabled": True,
    }
    hostile_unselected = {
        "ticker": "UNSELECTED",
        "publication_tier": "PAPER_PLAN_QUALIFIED",
        "research_only": False,
        "broker_execution": "live",
        "broker_execution_enabled": True,
    }

    published = apply_publication_semantics(
        [hostile_selected, hostile_unselected], slate=slate
    )
    assert all(row["research_only"] is True for row in published)
    assert all(row["broker_execution"] == "disabled" for row in published)
    assert all(row["broker_execution_enabled"] is False for row in published)
    assert all(row["publication_tier"] is None for row in published)
    assert publication_counts(published) == {
        "ranked_research": 0,
        "paper_plan_qualified": 0,
        "alertable_trade": 0,
        "official_selected": 0,
    }


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
