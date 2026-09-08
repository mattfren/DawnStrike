"""Tests for mover snapshot date validation fixes."""
import csv
import hashlib
import json
from pathlib import Path

import pytest

from intraday_scanner.models import SNAPSHOT_COLUMNS
from intraday_scanner.services.luna_core_universe_service import _canonical_member_hash
from intraday_scanner.v2.paper_ops.universe_handoff import (
    UniverseHandoffError,
    _expected_strategy_ids,
)
from intraday_scanner.v2.paper_ops.universe_handoff import (
    build_universe_handoff as _build_universe_handoff,
)

MARKET_DATE = "2026-08-28"
PRIOR_DATE = "2026-08-25"  # Friday before Tuesday


def build_universe_handoff(
    root: Path, market_date: str, *, output_path: Path | None = None
) -> dict[str, object]:
    """Use the explicit fixture trust injection for synthetic Morning inputs."""

    return _build_universe_handoff(
        root,
        market_date,
        output_path=output_path,
        allow_test_override=True,
    )


def _core_contract() -> dict[str, object]:
    """Create a minimal core universe contract for testing."""
    payload: dict[str, object] = {
        "schema_version": "dawnstrike.luna.core_universe.v1",
        "requested_market_date": MARKET_DATE,
        "observed_at": f"{MARKET_DATE}T12:00:00+00:00",
        "status": "READY",
        "completeness_verdict": "COMPLETE",
        "freshness_verdict": "FRESH",
        "contract_id": "luna-core-fixture",
        "content_hash_sha256": "",
        "content_hash": "",
        "universe_id": "luna-core-fixture",
        "membership_count": 1,
        "canonical_member_set_hash_sha256": "",
        "members": [
            {
                "symbol": "AAA",
                "index_memberships": ["Nasdaq-100", "S&P 500"],
                "sources": ["fixture-core"],
                "valid_from": MARKET_DATE,
            }
        ],
    }
    payload["canonical_member_set_hash_sha256"] = _canonical_member_hash(
        [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": "S&P 500",
                "valid_from": MARKET_DATE,
                "valid_to": None,
            }
        ]
        + [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": "Nasdaq-100",
                "valid_from": MARKET_DATE,
                "valid_to": None,
            }
        ]
    )
    payload["source_ids"] = ["fixture-spy", "fixture-ndx"]
    payload["source_uris"] = ["https://example.test/spy", "https://example.test/ndx"]
    payload["source_artifacts"] = [
        {
            "source_id": "fixture-spy",
            "source_uri": "https://example.test/spy",
            "raw_artifact_hashes": ["a" * 64],
            "canonical_member_set_hash_sha256": _canonical_member_hash(
                [
                    {
                        "symbol": "AAA",
                        "provider_symbol": "AAA",
                        "asset_class": "common_stock",
                        "index": "S&P 500",
                        "valid_from": MARKET_DATE,
                        "valid_to": None,
                    }
                ]
            ),
            "source_binding": {
                "status": "VERIFIED",
                "authority": "fixture",
                "index": "S&P 500",
                "transformation_id": "fixture-v1",
                "source_scope": "fixture S&P 500",
                "derived_member_set_hash_sha256": _canonical_member_hash(
                    [
                        {
                            "symbol": "AAA",
                            "provider_symbol": "AAA",
                            "asset_class": "common_stock",
                            "index": "S&P 500",
                            "valid_from": MARKET_DATE,
                            "valid_to": None,
                        }
                    ]
                ),
                "derived_membership_count": 1,
            },
        },
        {
            "source_id": "fixture-ndx",
            "source_uri": "https://example.test/ndx",
            "raw_artifact_hashes": ["b" * 64],
            "canonical_member_set_hash_sha256": _canonical_member_hash(
                [
                    {
                        "symbol": "AAA",
                        "provider_symbol": "AAA",
                        "asset_class": "common_stock",
                        "index": "Nasdaq-100",
                        "valid_from": MARKET_DATE,
                        "valid_to": None,
                    }
                ]
            ),
            "source_binding": {
                "status": "VERIFIED",
                "authority": "fixture",
                "index": "Nasdaq-100",
                "transformation_id": "fixture-v1",
                "source_scope": "fixture Nasdaq-100",
                "derived_member_set_hash_sha256": _canonical_member_hash(
                    [
                        {
                            "symbol": "AAA",
                            "provider_symbol": "AAA",
                            "asset_class": "common_stock",
                            "index": "Nasdaq-100",
                            "valid_from": MARKET_DATE,
                            "valid_to": None,
                        }
                    ]
                ),
                "derived_membership_count": 1,
            },
        },
    ]
    payload["index_verdicts"] = {
        "S&P 500": {
            "status": "READY",
            "expected_count": 1,
            "observed_unique_count": 1,
            "count_verdict": "PASS",
            "freshness_verdict": "FRESH",
            "effective_date_verdict": "PASS",
            "completeness_verdict": "COMPLETE",
        },
        "Nasdaq-100": {
            "status": "READY",
            "expected_count": 1,
            "observed_unique_count": 1,
            "count_verdict": "PASS",
            "freshness_verdict": "FRESH",
            "effective_date_verdict": "PASS",
            "completeness_verdict": "COMPLETE",
        },
    }
    # Compute hash as per the contract specification
    unhashed = dict(payload)
    for key in ("content_hash_sha256", "content_hash", "contract_id", "universe_id"):
        unhashed.pop(key, None)
    digest = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    payload["content_hash_sha256"] = digest
    payload["content_hash"] = digest
    return payload


def _morning_root(tmp_path: Path, *, snapshot_rows: list[dict[str, str]] | None = None) -> Path:
    """Create a morning root directory with optional custom snapshot rows."""
    root = tmp_path / "morning"
    (root / "web_collect").mkdir(parents=True)
    core = _core_contract()
    (root / "core_universe_contract.json").write_text(
        json.dumps(core, sort_keys=True), encoding="utf-8"
    )

    # Write snapshot with custom rows if provided
    source_path = root / "web_collect" / "premarket_snapshot.csv"
    with source_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS)
        writer.writeheader()
        if snapshot_rows:
            for row in snapshot_rows:
                writer.writerow(row)
        else:
            # Default: valid rows with market_date
            writer.writerow({"ticker": "AAA", "market_date": MARKET_DATE, "source": "mover"})
            writer.writerow({"ticker": "BBB", "market_date": MARKET_DATE, "source": "mover"})

    source = {
        "status": "success",
        "run_id": "mover-run",
        "candidate_count": len(snapshot_rows or [{"ticker": "AAA"}, {"ticker": "BBB"}]),
        "sources_attempted": 1,
        "sources_succeeded": 1,
        "source_failures": 0,
        "attempts": [
            {
                "source": "fixture_mover",
                "status": "success",
                "failure_reason": "",
            }
        ],
        "snapshot_path": str(source_path),
        "requested_observed_at": f"{MARKET_DATE}T12:00:00+00:00",
    }
    (root / "web_collect" / "source_summary.json").write_text(
        json.dumps(source, sort_keys=True), encoding="utf-8"
    )

    contract = {
        "schema_version": "alphaops.run_contract.v1",
        "producer": "alphaops",
        "producer_run_id": "scan-fixture",
        "market_date": MARKET_DATE,
        "generated_at": f"{MARKET_DATE}T12:00:00+00:00",
        "source_status": "success",
        "code_sha": "a" * 40,
    }
    (root / "alpha_run_contract.json").write_text(
        json.dumps(contract, sort_keys=True), encoding="utf-8"
    )

    cycle = {
        "scan_id": "scan-fixture",
        "generated_at": f"{MARKET_DATE}T12:00:00+00:00",
        "code_sha": "a" * 40,
        "source_summary": {
            **source,
            "code_sha": "a" * 40,
            "morning_strategy_adapter": {
                "enabled_strategy_ids": list(_expected_strategy_ids()),
            },
        },
    }
    (root / "alpha_cycle.json").write_text(json.dumps(cycle, sort_keys=True), encoding="utf-8")
    return root


def test_prior_last_trade_time_but_current_extraction_is_accepted(tmp_path: Path) -> None:
    """Test that a security that hasn't traded today but was extracted today is accepted.

    This is the TSLA/AVGO case from 2026-09-08: they last traded on Friday 2026-09-04
    but were extracted on Tuesday 2026-09-08 premarket. The validator must use extracted_at
    (the collection time), not as_of_timestamp (last trade time).
    """
    root = _morning_root(
        tmp_path,
        snapshot_rows=[
            {
                "ticker": "TSLA",
                "source": "web_collect",
                "as_of_timestamp": "2026-08-25T19:59:56Z",  # Friday close
                "extracted_at": f"{MARKET_DATE}T13:00:12+00:00",  # Tuesday premarket collection
                "market_date": "",  # Empty market_date to test fallback
            },
            {
                "ticker": "AVGO",
                "source": "web_collect",
                "as_of_timestamp": "2026-08-25T19:59:59Z",  # Friday close
                "extracted_at": f"{MARKET_DATE}T13:00:12+00:00",  # Tuesday premarket collection
                "market_date": "",  # Empty market_date to test fallback
            },
        ],
    )

    # Should succeed because extracted_at is today, even though as_of_timestamp is old
    result = build_universe_handoff(root, MARKET_DATE)
    # Core contract includes AAA, mover snapshot includes AVGO and TSLA
    assert sorted(result["universe_symbols"]) == ["AAA", "AVGO", "TSLA"]


def test_genuinely_stale_row_with_no_market_date_is_rejected(tmp_path: Path) -> None:
    """Test that a row from a prior date without market_date is rejected.

    If both extracted_at and as_of_timestamp are old, and market_date is missing,
    the row must be rejected. This validates that the fix doesn't blind the validator.
    """
    root = _morning_root(
        tmp_path,
        snapshot_rows=[
            {
                "ticker": "OLD",
                "source": "web_collect",
                "as_of_timestamp": f"{PRIOR_DATE}T19:59:56Z",  # Friday
                "extracted_at": f"{PRIOR_DATE}T13:00:12+00:00",  # Friday extraction
                "market_date": "",  # Empty - both timestamps are old
            },
        ],
    )

    with pytest.raises(UniverseHandoffError, match="row date is missing"):
        build_universe_handoff(root, MARKET_DATE)


def test_cross_date_market_date_is_rejected(tmp_path: Path) -> None:
    """Test that a row with market_date from a different date is rejected."""
    root = _morning_root(
        tmp_path,
        snapshot_rows=[
            {
                "ticker": "BAD",
                "source": "web_collect",
                "market_date": PRIOR_DATE,  # Wrong date
                "as_of_timestamp": f"{MARKET_DATE}T13:00:12Z",
                "extracted_at": f"{MARKET_DATE}T13:00:12+00:00",
            },
        ],
    )

    with pytest.raises(UniverseHandoffError, match="cross-date"):
        build_universe_handoff(root, MARKET_DATE)


def test_a_declared_market_date_bypasses_the_timestamp_check(tmp_path: Path) -> None:
    """A populated ``market_date`` short-circuits the per-row freshness evidence.

    This pins why the collector must NOT blanket-stamp every row with the
    session date. When ``market_date`` matches the requested date, neither
    branch of the row-date check examines ``extracted_at`` or
    ``as_of_timestamp`` - so a row whose actual observation is days old is
    accepted without challenge, as demonstrated below.

    Leaving the column to the collector's real per-row evidence keeps
    ``extracted_at`` load-bearing, which is what actually catches a stale row.
    """

    root = _morning_root(
        tmp_path,
        snapshot_rows=[
            {
                "ticker": "STALE",
                "source": "web_collect",
                "market_date": MARKET_DATE,  # claims today ...
                "as_of_timestamp": f"{PRIOR_DATE}T19:59:56Z",  # ... but is three days old
                "extracted_at": f"{PRIOR_DATE}T13:00:12+00:00",
            },
        ],
    )

    result = build_universe_handoff(root, MARKET_DATE)
    assert "STALE" in result["universe_symbols"]


def test_extracted_at_is_what_catches_a_stale_row(tmp_path: Path) -> None:
    """The counterpart: with no declared date, stale evidence is caught."""

    root = _morning_root(
        tmp_path,
        snapshot_rows=[
            {
                "ticker": "STALE",
                "source": "web_collect",
                "market_date": "",
                "as_of_timestamp": f"{PRIOR_DATE}T19:59:56Z",
                "extracted_at": f"{PRIOR_DATE}T13:00:12+00:00",
            },
        ],
    )

    with pytest.raises(UniverseHandoffError, match="row date is missing"):
        build_universe_handoff(root, MARKET_DATE)
