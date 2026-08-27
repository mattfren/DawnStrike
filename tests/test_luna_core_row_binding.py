import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.scoring import score_snapshot
from intraday_scanner.services import luna_core_universe_service as core
from intraday_scanner.services.premarket_enrichment_service import _apply_observation

OBSERVED_AT = datetime(2026, 1, 5, 13, 5, tzinfo=timezone.utc)


def _discovery_result() -> dict:
    class Provider:
        def validate_credentials(self):
            return None

        def get_premarket_snapshot(self, symbols, config):
            return [
                {
                    "ticker": symbols[0],
                    "source": "alpaca_iex",
                    "source_timestamp": "2026-01-05T13:04:30+00:00",
                    "premarket_price": 10,
                    "premarket_volume": 100,
                    "dollar_volume": 1000,
                    "previous_close": 9,
                    "premarket_high": 10,
                    "premarket_low": 9,
                }
            ]

    return core.discover_core_universe_rows(
        {
            "status": "READY",
            "content_hash_sha256": "c" * 64,
            "members": [{"symbol": "BOUND", "index_memberships": ["S&P 500"]}],
        },
        config=SimpleNamespace(premarket_enrichment_max_age_seconds=600),
        provider=Provider(),
        observed_at=OBSERVED_AT,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("premarket_price", 11),
        ("premarket_volume", 101),
        ("dollar_volume", 1100),
        ("gap_pct", 99),
        ("source", "yahoo_finance"),
        ("source_timestamp", "2026-01-05T13:00:00+00:00"),
        ("as_of_timestamp", "2026-01-05T13:00:00+00:00"),
        ("ticker", "OTHER"),
        ("core_universe_memberships", ["Nasdaq-100"]),
        ("freshness_status", "STALE"),
        ("stale_data_flag", True),
        ("core_lane_score", 999),
    ],
)
def test_hostile_core_row_mutation_fails_closed_at_ranking_boundary(field, value) -> None:
    result = _discovery_result()
    row = dict(result["rows"][0])

    assert result["coverage_receipts"][0]["row_binding_hashes"] == {
        "BOUND": row[core.CORE_COVERAGE_ROW_BINDING_FIELD]
    }
    row[field] = value

    assert core.rank_core_universe_rows([row]) == []


def test_core_row_binding_survives_json_and_csv_string_numeric_roundtrip(tmp_path: Path) -> None:
    result = _discovery_result()
    original = dict(result["rows"][0])
    receipt = result["coverage_receipts"][0]

    assert receipt["schema_version"] == core.CORE_COVERAGE_RECEIPT_SCHEMA_VERSION
    assert receipt["row_binding_schema_version"] == (
        core.CORE_COVERAGE_ROW_PROJECTION_SCHEMA_VERSION
    )
    assert core.rank_core_universe_rows([json.loads(json.dumps(original))])

    path = core.write_snapshot_rows([original], tmp_path / "core.csv")
    loaded = read_snapshot_csv(path)[0].to_dict()

    assert (
        loaded[core.CORE_COVERAGE_ROW_BINDING_FIELD]
        == original[core.CORE_COVERAGE_ROW_BINDING_FIELD]
    )
    assert loaded["premarket_price"] == 10.0
    assert loaded["premarket_volume"] == 100
    assert loaded["gap_pct"] == pytest.approx(11.1111111111)
    assert loaded["core_universe_memberships"] == "['S&P 500']"
    ranked = core.rank_core_universe_rows([loaded])
    assert [row["ticker"] for row in ranked] == ["BOUND"]
    assert ranked[0]["core_lane_score"] == 1000.0


def test_receipt_response_hash_covers_raw_provider_rows_before_decoration() -> None:
    result = _discovery_result()
    raw_provider_row = {
        "ticker": "BOUND",
        "source": "alpaca_iex",
        "source_timestamp": "2026-01-05T13:04:30+00:00",
        "premarket_price": 10,
        "premarket_volume": 100,
        "dollar_volume": 1000,
        "previous_close": 9,
        "premarket_high": 10,
        "premarket_low": 9,
    }
    response_hash = result["coverage_receipts"][0]["response_hash_sha256"]

    assert response_hash == core._snapshot_response_hash([raw_provider_row])
    assert response_hash != core._snapshot_response_hash([result["rows"][0]])


def test_merge_preserves_core_row_binding_field() -> None:
    result = _discovery_result()
    core_row = result["rows"][0]

    merged = core.merge_core_universe_rows(
        [{"ticker": "BOUND", "discovery_context": "mover"}], [core_row]
    )

    assert (
        merged[0][core.CORE_COVERAGE_ROW_BINDING_FIELD]
        == core_row[core.CORE_COVERAGE_ROW_BINDING_FIELD]
    )


def test_core_binding_rejects_source_confidence_mutation() -> None:
    row = dict(_discovery_result()["rows"][0])
    assert row["source_confidence"] == 80.0
    assert core._core_coverage_binding_valid(row)

    row["source_confidence"] = 100.0

    assert not core._core_coverage_binding_valid(row)
    assert core.rank_core_universe_rows([row]) == []


def test_scored_core_row_preserves_receipt_bound_source_confidence(tmp_path: Path) -> None:
    row = dict(_discovery_result()["rows"][0])
    snapshot = read_snapshot_csv(
        core.write_snapshot_rows([row], tmp_path / "scored-core.csv")
    )[0]

    scored = score_snapshot(snapshot, ScannerConfig(), as_of=OBSERVED_AT).to_dict()

    assert scored["source_confidence"] == 80.0
    assert scored["model_adjusted_source_confidence"] != scored["source_confidence"]
    assert scored["stale_data_flag"] is False
    assert "stale_data" not in scored["data_warnings"]
    assert core._core_coverage_binding_valid(scored)


def test_fully_stripped_core_row_cannot_enter_legacy_fallback() -> None:
    row = dict(_discovery_result()["rows"][0])
    for key in (
        "core_coverage_receipt_id",
        "core_coverage_receipt_hash_sha256",
        "core_coverage_receipt_status",
        "core_coverage_receipt_payload_json",
        core.CORE_COVERAGE_ROW_BINDING_FIELD,
        "universe_lane",
        "evidence_lane",
        "discovery_context",
        "core_universe_memberships",
    ):
        row.pop(key, None)

    assert core.rank_core_universe_rows([row]) == []


def test_failed_optional_range_enrichment_preserves_core_binding() -> None:
    result = _discovery_result()
    row = dict(result["rows"][0])

    enriched = _apply_observation(
        row,
        None,
        enabled=True,
        selected=True,
        primary_source="alpaca_market_data_iex",
        fallback_status="attempted_unusable",
    )

    assert enriched["enrichment_status"] == "provider_error"
    assert core._core_coverage_binding_valid(enriched)
    assert [item["ticker"] for item in core.rank_core_universe_rows([enriched])] == ["BOUND"]


def test_missing_gap_without_previous_close_roundtrips_as_absence(tmp_path: Path) -> None:
    class Provider:
        def validate_credentials(self):
            return None

        def get_premarket_snapshot(self, symbols, config):
            return [
                {
                    "ticker": symbols[0],
                    "source": "alpaca_iex",
                    "source_timestamp": "2026-01-05T13:04:30+00:00",
                    "premarket_price": 10,
                    "premarket_volume": 100,
                    "premarket_high": 10,
                    "premarket_low": 9,
                }
            ]

    result = core.discover_core_universe_rows(
        {
            "status": "READY",
            "members": [{"symbol": "BOUND", "index_memberships": ["S&P 500"]}],
        },
        config=SimpleNamespace(premarket_enrichment_max_age_seconds=600),
        provider=Provider(),
        observed_at=OBSERVED_AT,
    )
    row = dict(result["rows"][0])
    assert row.get("gap_pct") is None
    assert row["dollar_volume"] == 1000.0
    assert core.rank_core_universe_rows([row])

    loaded = read_snapshot_csv(core.write_snapshot_rows([row], tmp_path / "missing-gap.csv"))[0]
    loaded_row = loaded.to_dict()

    assert loaded_row["gap_pct"] == 0.0
    assert core._core_coverage_binding_valid(loaded_row)
    assert core.rank_core_universe_rows([loaded_row])


def test_duplicate_ticker_has_no_ambiguous_row_binding() -> None:
    class Provider:
        def validate_credentials(self):
            return None

        def get_premarket_snapshot(self, symbols, config):
            return [
                {
                    "ticker": "DUP",
                    "source": "alpaca_iex",
                    "source_timestamp": "2026-01-05T13:04:30+00:00",
                    "premarket_price": 10,
                    "premarket_volume": 100,
                },
                {
                    "ticker": "DUP",
                    "source": "alpaca_iex",
                    "source_timestamp": "2026-01-05T13:04:29+00:00",
                    "premarket_price": 10,
                    "premarket_volume": 100,
                },
            ]

    result = core.discover_core_universe_rows(
        {
            "status": "READY",
            "members": [{"symbol": "DUP", "index_memberships": ["S&P 500"]}],
        },
        config=SimpleNamespace(premarket_enrichment_max_age_seconds=600),
        provider=Provider(),
        observed_at=OBSERVED_AT,
    )
    receipt = result["coverage_receipts"][0]

    assert receipt["duplicate_symbols"] == ["DUP"]
    assert "DUP" not in receipt["row_binding_hashes"]
    assert receipt["row_bindings"] == []
    assert result["rows"] == []
