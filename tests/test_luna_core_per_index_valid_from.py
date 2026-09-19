"""DS-03: per-index membership validity must not collapse to one fact.

`luna_core_universe_service.py` used to key merged member rows by symbol
alone and fold each index's `valid_from`/`valid_to` together with
``max()``.  For a symbol that belongs to two indexes with different
effective dates, that made the merged row false for at least one index,
and it also had an independent bug: merging an open-ended membership
(``valid_to`` empty/None) with a closed one silently stamped an expiry on
a still-current membership, because ``max("", "2024-01-01")`` returns the
dated string.

These tests pin the corrected behaviour: each index's validity survives
untouched in ``index_validity``, per-index projection hashes are
reproducible and input-order independent, an open membership can never be
closed by merging, and tamper/duplicate detection keeps failing closed.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from intraday_scanner.services import luna_core_universe_service as core
from intraday_scanner.v2.paper_ops import universe_handoff


def _manifest(
    *,
    source_id: str,
    index_name: str,
    effective_date: str,
    members: list[dict[str, Any]],
    observed_at: str = "2024-01-02T12:00:00Z",
) -> dict[str, Any]:
    raw_content = f"fixture-raw-artifact:{source_id}:{index_name}:{effective_date}"
    return {
        "source_id": source_id,
        "source_uri": f"https://example.test/{source_id}",
        "observed_at": observed_at,
        "effective_date": effective_date,
        "index_name": index_name,
        "expected_count": len(members),
        "members": members,
        "raw_artifact_content": raw_content,
        "raw_artifact_sha256": hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
    }


def _two_index_manifests(*, spx_valid_from: str, ndx_valid_from: str, ndx_valid_to=None):
    spx = _manifest(
        source_id="spx-src",
        index_name="S&P 500",
        effective_date="2024-01-02",
        members=[{"ticker": "AAA", "index": "S&P 500", "valid_from": spx_valid_from}],
    )
    ndx = _manifest(
        source_id="ndx-src",
        index_name="Nasdaq-100",
        effective_date="2024-01-02",
        members=[
            {
                "ticker": "AAA",
                "index": "Nasdaq-100",
                "valid_from": ndx_valid_from,
                "valid_to": ndx_valid_to,
            }
        ],
    )
    return spx, ndx


def _index_hash(contract: dict[str, Any], index: str) -> str:
    records = [
        {
            "symbol": row["symbol"],
            "provider_symbol": row.get("provider_symbol"),
            "asset_class": row.get("asset_class"),
            "index": index,
            "valid_from": row["index_validity"][index]["valid_from"],
            "valid_to": row["index_validity"][index]["valid_to"],
        }
        for row in contract["members"]
        if index in row["index_memberships"]
    ]
    return core._canonical_member_hash(records)


def test_two_indexes_with_different_valid_from_hash_independently_and_order_free() -> None:
    spx, ndx = _two_index_manifests(spx_valid_from="2020-01-01", ndx_valid_from="2023-06-01")

    forward = core.build_core_universe_contract(
        [spx, ndx],
        observed_at="2024-01-02T13:00:00Z",
        market_date="2024-01-02",
        allow_test_override=True,
    )
    reverse = core.build_core_universe_contract(
        [ndx, spx],
        observed_at="2024-01-02T13:00:00Z",
        market_date="2024-01-02",
        allow_test_override=True,
    )

    for contract in (forward, reverse):
        row = next(r for r in contract["members"] if r["symbol"] == "AAA")
        assert row["index_validity"]["S&P 500"]["valid_from"] == "2020-01-01"
        assert row["index_validity"]["Nasdaq-100"]["valid_from"] == "2023-06-01"

    expected_spx_hash = core._canonical_member_hash(
        [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": "S&P 500",
                "valid_from": "2020-01-01",
                "valid_to": None,
            }
        ]
    )
    expected_ndx_hash = core._canonical_member_hash(
        [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": "Nasdaq-100",
                "valid_from": "2023-06-01",
                "valid_to": None,
            }
        ]
    )

    for contract in (forward, reverse):
        assert _index_hash(contract, "S&P 500") == expected_spx_hash
        assert _index_hash(contract, "Nasdaq-100") == expected_ndx_hash

    # Order independence: forward vs. reverse manifest load order must not
    # change either index's projection hash.
    assert _index_hash(forward, "S&P 500") == _index_hash(reverse, "S&P 500")
    assert _index_hash(forward, "Nasdaq-100") == _index_hash(reverse, "Nasdaq-100")
    # And the two indexes must NOT hash the same as each other, since they
    # disagree on valid_from.
    assert _index_hash(forward, "S&P 500") != _index_hash(forward, "Nasdaq-100")


def test_open_ended_membership_is_not_closed_by_a_dated_sibling_index() -> None:
    # S&P 500 membership is closed 2023-01-01; Nasdaq-100 membership is
    # open-ended.  Request a date both memberships actually cover.
    spx, ndx = _two_index_manifests(spx_valid_from="2020-01-01", ndx_valid_from="2020-01-01")
    spx["members"][0]["valid_to"] = "2023-01-01"
    # ndx member has no valid_to => open-ended.

    contract = core.build_core_universe_contract(
        [spx, ndx],
        observed_at="2022-06-15T13:00:00Z",
        market_date="2022-06-01",
        allow_test_override=True,
    )
    row = next(r for r in contract["members"] if r["symbol"] == "AAA")

    assert row["index_validity"]["S&P 500"]["valid_to"] == "2023-01-01"
    assert row["index_validity"]["Nasdaq-100"]["valid_to"] is None
    # The collapsed compat field must stay open too -- merging in the
    # closed S&P 500 fact must never truncate the open Nasdaq-100 fact.
    assert row["valid_to"] is None


MARKET_DATE = "2026-08-28"


def _manual_core_contract(*, spx_valid_from: str, ndx_valid_from: str) -> dict[str, Any]:
    """Hand-build a READY core contract the way the handoff validator expects.

    Mirrors ``tests/test_paperops_universe_handoff.py::_core_contract`` (the
    existing fixture pattern for exercising ``_validate_core_contract``
    directly, with ``authority: "fixture"`` standing in for a real trust
    root), but gives the single overlapping member two genuinely different
    per-index ``valid_from`` dates via ``index_validity`` -- the shape DS-03
    requires and the collapsed-field bug could never represent correctly.
    """

    index_validity = {
        "S&P 500": {"valid_from": spx_valid_from, "valid_to": None},
        "Nasdaq-100": {"valid_from": ndx_valid_from, "valid_to": None},
    }
    payload: dict[str, Any] = {
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
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index_memberships": ["Nasdaq-100", "S&P 500"],
                "sources": ["fixture-core"],
                "index_validity": index_validity,
                # Collapsed compat field: deliberately NOT equal to either
                # per-index value, to prove the hash reads index_validity
                # and not this field.
                "valid_from": min(spx_valid_from, ndx_valid_from),
                "valid_to": None,
            }
        ],
    }
    payload["canonical_member_set_hash_sha256"] = core._canonical_member_hash(
        [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": index,
                "valid_from": index_validity[index]["valid_from"],
                "valid_to": index_validity[index]["valid_to"],
            }
            for index in ("S&P 500", "Nasdaq-100")
        ]
    )
    payload["source_ids"] = ["fixture-spy", "fixture-ndx"]
    payload["source_uris"] = ["https://example.test/spy", "https://example.test/ndx"]
    payload["source_artifacts"] = [
        {
            "source_id": "fixture-spy",
            "source_uri": "https://example.test/spy",
            "raw_artifact_hashes": ["a" * 64],
            "canonical_member_set_hash_sha256": core._canonical_member_hash(
                [
                    {
                        "symbol": "AAA",
                        "provider_symbol": "AAA",
                        "asset_class": "common_stock",
                        "index": "S&P 500",
                        "valid_from": index_validity["S&P 500"]["valid_from"],
                        "valid_to": index_validity["S&P 500"]["valid_to"],
                    }
                ]
            ),
            "source_binding": {
                "status": "VERIFIED",
                "authority": "fixture",
                "index": "S&P 500",
                "transformation_id": "fixture-v1",
                "source_scope": "fixture S&P 500",
                "derived_member_set_hash_sha256": core._canonical_member_hash(
                    [
                        {
                            "symbol": "AAA",
                            "provider_symbol": "AAA",
                            "asset_class": "common_stock",
                            "index": "S&P 500",
                            "valid_from": index_validity["S&P 500"]["valid_from"],
                            "valid_to": index_validity["S&P 500"]["valid_to"],
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
            "canonical_member_set_hash_sha256": core._canonical_member_hash(
                [
                    {
                        "symbol": "AAA",
                        "provider_symbol": "AAA",
                        "asset_class": "common_stock",
                        "index": "Nasdaq-100",
                        "valid_from": index_validity["Nasdaq-100"]["valid_from"],
                        "valid_to": index_validity["Nasdaq-100"]["valid_to"],
                    }
                ]
            ),
            "source_binding": {
                "status": "VERIFIED",
                "authority": "fixture",
                "index": "Nasdaq-100",
                "transformation_id": "fixture-v1",
                "source_scope": "fixture Nasdaq-100",
                "derived_member_set_hash_sha256": core._canonical_member_hash(
                    [
                        {
                            "symbol": "AAA",
                            "provider_symbol": "AAA",
                            "asset_class": "common_stock",
                            "index": "Nasdaq-100",
                            "valid_from": index_validity["Nasdaq-100"]["valid_from"],
                            "valid_to": index_validity["Nasdaq-100"]["valid_to"],
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
    unhashed = dict(payload)
    for key in ("content_hash_sha256", "content_hash", "contract_id", "universe_id"):
        unhashed.pop(key, None)
    digest = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    payload["content_hash_sha256"] = digest
    payload["content_hash"] = digest
    return payload


def test_healthy_multi_index_fixture_delivers_nonempty_core_universe() -> None:
    contract = _manual_core_contract(spx_valid_from="2020-01-01", ndx_valid_from="2023-06-01")

    core_ready_indexes = universe_handoff._validate_core_contract(
        contract, MARKET_DATE, allow_test_override=True
    )
    assert core_ready_indexes == {"S&P 500", "Nasdaq-100"}
    core_included = universe_handoff._core_members(contract, allowed_indexes=core_ready_indexes)
    assert len(core_included) > 0


def test_duplicate_member_global_still_fires_within_one_index() -> None:
    spx = _manifest(
        source_id="spx-src",
        index_name="S&P 500",
        effective_date="2024-01-02",
        members=[
            {"ticker": "AAA", "index": "S&P 500", "valid_from": "2020-01-01"},
        ],
    )
    spx_dupe = _manifest(
        source_id="spx-src-2",
        index_name="S&P 500",
        effective_date="2024-01-02",
        members=[
            {"ticker": "AAA", "index": "S&P 500", "valid_from": "2020-01-01"},
        ],
    )
    contract = core.build_core_universe_contract(
        [spx, spx_dupe],
        observed_at="2024-01-02T13:00:00Z",
        market_date="2024-01-02",
        allow_test_override=True,
    )
    assert any(item.startswith("duplicate_member_global:S&P 500:AAA") for item in contract["blockers"])


def _tamperable_healthy_contract() -> dict[str, Any]:
    contract = _manual_core_contract(spx_valid_from="2020-01-01", ndx_valid_from="2023-06-01")
    universe_handoff._validate_core_contract(contract, MARKET_DATE, allow_test_override=True)
    return contract


def test_tampered_per_index_valid_from_fails_the_hash_check() -> None:
    contract = copy.deepcopy(_tamperable_healthy_contract())
    row = next(r for r in contract["members"] if r["symbol"] == "AAA")
    # Attacker back-dates the Nasdaq-100 membership without updating the
    # declared canonical member set hash.
    row["index_validity"]["Nasdaq-100"]["valid_from"] = "1999-01-01"

    try:
        universe_handoff._validate_core_contract(contract, MARKET_DATE, allow_test_override=True)
    except universe_handoff.UniverseHandoffError as exc:
        assert "hash" in str(exc)
    else:
        raise AssertionError("tampered per-index valid_from must fail the hash check")


def test_tampered_member_symbol_fails_the_hash_check() -> None:
    contract = copy.deepcopy(_tamperable_healthy_contract())
    row = next(r for r in contract["members"] if r["symbol"] == "AAA")
    row["symbol"] = "ZZZ"

    try:
        universe_handoff._validate_core_contract(contract, MARKET_DATE, allow_test_override=True)
    except universe_handoff.UniverseHandoffError:
        pass
    else:
        raise AssertionError("tampered member symbol must fail closed")


def test_wrong_member_set_fails_closed() -> None:
    contract = copy.deepcopy(_tamperable_healthy_contract())
    row = next(r for r in contract["members"] if r["symbol"] == "AAA")
    extra = copy.deepcopy(row)
    extra["symbol"] = "BOGUS"
    contract["members"].append(extra)
    contract["membership_count"] = len(contract["members"])

    try:
        universe_handoff._validate_core_contract(contract, MARKET_DATE, allow_test_override=True)
    except universe_handoff.UniverseHandoffError:
        pass
    else:
        raise AssertionError("an unexpected extra member must fail closed")
