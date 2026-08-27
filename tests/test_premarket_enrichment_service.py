import csv
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SnapshotRow
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.scoring import score_snapshot
from intraday_scanner.services import premarket_enrichment_service as enrichment_service
from intraday_scanner.services.luna_core_universe_service import write_snapshot_rows
from intraday_scanner.services.luna_research_slate_service import build_ranked_research_slate
from intraday_scanner.services.premarket_enrichment_service import (
    PremarketObservation,
    enrich_premarket_rows,
    observation_from_alpaca_bars,
    observation_from_chart_payload,
)
from intraday_scanner.services.premarket_intelligence import field_sources

UTC = timezone.utc


def test_verified_premarket_observation_requires_completed_bar_to_be_usable():
    observation = PremarketObservation(
        ticker="NOVA",
        status="verified",
        premarket_high=8.2,
        premarket_low=7.8,
    )

    assert observation.is_usable is False


def _payload(*, timestamps, highs, lows, closes, previous_close=5.0):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "chartPreviousClose": previous_close,
                        "exchangeTimezoneName": "America/New_York",
                        "currentTradingPeriod": {
                            "pre": {
                                "start": int(
                                    datetime(2026, 7, 13, 8, 0, tzinfo=UTC).timestamp()
                                ),
                                "end": int(
                                    datetime(2026, 7, 13, 13, 30, tzinfo=UTC).timestamp()
                                ),
                            }
                        },
                    },
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": closes,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": [1000 for _ in timestamps],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def _epoch(hour, minute):
    return int(datetime(2026, 7, 13, hour, minute, tzinfo=UTC).timestamp())


def _row(**overrides):
    row = {
        "ticker": "NOVA",
        "company": "Nova Research",
        "premarket_price": 8.0,
        "previous_close": "",
        "premarket_high": 8.0,
        "premarket_low": 8.0,
        "premarket_volume": 500_000,
        "dollar_volume": 4_000_000,
        "gap_pct": 60.0,
        "fixture_only": False,
        "manual_uploaded_data": False,
        "coverage_warning": (
            "previous_close_unavailable;premarket_range_unavailable_price_used;"
            "sec_risk_unverified"
        ),
    }
    row.update(overrides)
    return row


def test_chart_observation_is_point_in_time_and_excludes_future_bar():
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    payload = _payload(
        timestamps=[_epoch(12, 55), _epoch(13, 0), _epoch(13, 10)],
        highs=[8.2, 8.4, 99.0],
        lows=[7.8, 7.7, 0.01],
        closes=[8.0, 8.1, 50.0],
    )

    observation = observation_from_chart_payload(
        "NOVA",
        payload,
        requested_at=requested_at,
        max_age_seconds=1200,
    )

    assert observation.status == "verified"
    assert observation.premarket_high == 8.2
    assert observation.premarket_low == 7.8
    assert observation.previous_close == 5.0
    assert observation.bar_count == 1
    assert observation.observed_at == "2026-07-13T12:55:00+00:00"
    assert observation.bar_completed_at == "2026-07-13T12:56:00+00:00"
    assert observation.is_complete is True
    assert observation.is_usable is True


def test_enrichment_replaces_only_missing_plan_facts_with_sourced_observation(tmp_path):
    payload = _payload(
        timestamps=[_epoch(12, 55), _epoch(13, 0)],
        highs=[8.2, 8.4],
        lows=[7.8, 7.7],
        closes=[8.0, 8.1],
    )

    result = enrich_premarket_rows(
        [_row()],
        config=ScannerConfig(
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        fetcher=lambda _ticker, _config: payload,
        out_dir=tmp_path,
    )
    row = result["rows"][0]

    assert result["summary"]["status"] == "complete"
    assert row["premarket_high"] == 8.2
    assert row["premarket_low"] == 7.8
    assert row["previous_close"] == 5.0
    assert row["gap_pct"] == 60.0
    assert row["premarket_range_source"] == "yahoo_finance_chart"
    assert row["enrichment_status"] == "verified"
    assert row["freshness_status"] == ""
    assert "premarket_range_unavailable_price_used" not in row["coverage_warning"]
    assert "previous_close_unavailable" not in row["coverage_warning"]
    assert "sec_risk_unverified" in row["coverage_warning"]
    assert (tmp_path / "premarket_snapshot.csv").exists()
    assert (tmp_path / "observations.json").exists()


def test_stale_observation_keeps_missing_truth_ineligible():
    payload = _payload(
        timestamps=[_epoch(12, 0)],
        highs=[8.4],
        lows=[7.7],
        closes=[8.0],
    )

    result = enrich_premarket_rows(
        [_row()],
        config=ScannerConfig(premarket_enrichment_max_age_seconds=300),
        requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        fetcher=lambda _ticker, _config: payload,
    )
    row = result["rows"][0]

    assert result["summary"]["status"] == "unavailable"
    assert row["enrichment_status"] == "stale_observation"
    assert row["freshness_status"] == ""
    assert row["premarket_high"] == 8.0
    assert row["premarket_low"] == 8.0
    assert "premarket_range_unavailable_price_used" in row["coverage_warning"]


def test_fixture_rows_are_never_enriched_as_live_data():
    called = False

    def fetcher(_ticker, _config):
        nonlocal called
        called = True
        return {}

    result = enrich_premarket_rows(
        [_row(fixture_only=True)],
        config=ScannerConfig(),
        fetcher=fetcher,
    )

    assert called is False
    assert result["summary"]["status"] == "no_eligible_candidates"
    assert result["rows"][0]["enrichment_status"] == "not_selected"


def test_alpaca_enrichment_owns_price_range_volume_and_excludes_future_bar():
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    bars = [
        {
            "ticker": "NOVA",
            "timestamp": "2026-07-13T12:55:00Z",
            "high": 8.2,
            "low": 7.8,
            "close": 8.0,
            "volume": 1000,
        },
        {
            "ticker": "NOVA",
            "timestamp": "2026-07-13T13:00:00Z",
            "high": 8.4,
            "low": 7.7,
            "close": 8.1,
            "volume": 1500,
        },
        {
            "ticker": "NOVA",
            "timestamp": "2026-07-13T13:10:00Z",
            "high": 99.0,
            "low": 0.01,
            "close": 50.0,
            "volume": 999999,
        },
    ]

    observation = observation_from_alpaca_bars(
        "NOVA",
        bars,
        previous_close=5.0,
        requested_at=requested_at,
        max_age_seconds=1200,
        feed="iex",
    )

    assert observation.status == "verified"
    assert observation.premarket_high == 8.2
    assert observation.premarket_low == 7.8
    assert observation.latest_price == 8.0
    assert observation.premarket_volume == 1000
    assert observation.observed_at == "2026-07-13T12:55:00+00:00"
    assert observation.bar_completed_at == "2026-07-13T12:56:00+00:00"
    assert observation.is_complete is True
    assert observation.is_usable is True
    assert observation.source == "alpaca_market_data_iex"


def test_alphaops_alpaca_enrichment_replaces_public_page_price_facts():
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)

    class FakeAlpacaProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            assert symbols == ["NOVA"]
            assert config.alpaca_data_feed == "iex"
            return [
                {
                    "ticker": "NOVA",
                    "timestamp": "2026-07-13T12:55:00Z",
                    "high": 8.2,
                    "low": 7.8,
                    "close": 8.0,
                    "volume": 1000,
                },
                {
                    "ticker": "NOVA",
                    "timestamp": "2026-07-13T13:00:00Z",
                    "high": 8.4,
                    "low": 7.7,
                    "close": 8.1,
                    "volume": 1500,
                },
            ]

        def get_premarket_snapshot(self, symbols, config):
            return [SimpleNamespace(ticker="NOVA", previous_close=5.0)]

    result = enrich_premarket_rows(
        [_row(premarket_price=9.0, premarket_volume=1)],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=requested_at,
        source="alpaca",
        alpaca_provider=FakeAlpacaProvider(),
    )
    row = result["rows"][0]

    assert result["summary"]["source"] == "alpaca_market_data_iex"
    assert row["premarket_price"] == 8.0
    assert row["premarket_high"] == 8.2
    assert row["premarket_low"] == 7.8
    assert row["previous_close"] == 5.0
    assert row["premarket_volume"] == 1000
    assert row["dollar_volume"] == 8000.0
    assert row["gap_pct"] == 60.0
    assert row["premarket_range_source"] == "alpaca_market_data_iex"
    assert row["enrichment_is_complete"] is True
    assert row["enrichment_was_fallback"] is False
    assert row["freshness_status"] == "FRESH"


def test_authenticated_current_mover_freshness_survives_snapshot_model_roundtrip(
    tmp_path: Path,
):
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    source_timestamp = datetime.now(UTC).replace(microsecond=0).isoformat()

    class CurrentAlpacaProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return [
                {
                    "ticker": "NOVA",
                    "timestamp": "2026-07-13T12:58:00Z",
                    "high": 8.2,
                    "low": 7.8,
                    "close": 8.1,
                    "volume": 1500,
                }
            ]

        def get_premarket_snapshot(self, symbols, config):
            return [SimpleNamespace(ticker="NOVA", previous_close=5.0)]

    result = enrich_premarket_rows(
        [
            _row(
                source="alpaca_iex",
                preferred_source="alpaca_iex",
                as_of_timestamp=source_timestamp,
                source_timestamp=source_timestamp,
                source_quality_status="VERIFIED",
                source_count=1,
                halt_status="CLEAR",
                sec_risk_status="CLEAR",
                corporate_action_status="CLEAR",
                universe_lane="mover",
                evidence_lane="mover",
            )
        ],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=requested_at,
        source="alpaca",
        alpaca_provider=CurrentAlpacaProvider(),
    )
    row = result["rows"][0]
    assert row["enrichment_status"] == "verified"
    assert row["enrichment_is_complete"] is True
    assert row["freshness_status"] == "FRESH"
    assert len(row["enrichment_observation_sha256"]) == 64
    assert json.loads(row["enrichment_observation_payload_json"])["status"] == "verified"

    snapshot_path = write_snapshot_rows(result["rows"], tmp_path / "mover_snapshot.csv")
    snapshot = read_snapshot_csv(snapshot_path)[0]
    assert snapshot.freshness_status == "FRESH"
    assert snapshot.evidence_lane == "mover"
    assert snapshot.enrichment_observation_sha256 == row["enrichment_observation_sha256"]

    persisted_tampered = snapshot.to_dict()
    persisted_observation = json.loads(
        persisted_tampered["enrichment_observation_payload_json"]
    )
    persisted_observation["premarket_high"] = 99.0
    persisted_tampered["enrichment_observation_payload_json"] = json.dumps(
        persisted_observation,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    assert SnapshotRow.from_mapping(persisted_tampered).freshness_status == ""

    hostile_fields = {
        "premarket_price": 99.0,
        "premarket_volume": 999_999,
        "previous_close": 99.0,
        "dollar_volume": 99_999_999.0,
        "gap_pct": 999.0,
        "enrichment_observation_sha256": "0" * 64,
        "premarket_raw_payload_json": "{}",
        "premarket_source_hash_sha256": "0" * 64,
        "ticker": "EVIL",
    }
    for field, value in hostile_fields.items():
        hostile = snapshot.to_dict()
        hostile[field] = value
        assert SnapshotRow.from_mapping(hostile).freshness_status == ""

    candidate = score_snapshot(snapshot, ScannerConfig()).to_dict()
    assert candidate["freshness_status"] == "FRESH"
    assert candidate["evidence_lane"] == "mover"
    lineage = candidate["source_lineage"]
    assert lineage["freshness_status"] == "FRESH"
    assert lineage["premarket_observation"]["freshness_status"] == "FRESH"
    slate = build_ranked_research_slate(
        [candidate],
        target=1,
        require_safety=True,
        generated_at=source_timestamp,
    )
    assert slate["symbols"] == ["NOVA"]


def test_tampered_alpaca_observation_payload_does_not_claim_freshness(monkeypatch):
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    bars = [
        {
            "ticker": "NOVA",
            "timestamp": "2026-07-13T12:58:00Z",
            "high": 8.2,
            "low": 7.8,
            "close": 8.1,
            "volume": 1500,
        }
    ]
    valid = observation_from_alpaca_bars(
        "NOVA",
        bars,
        previous_close=5.0,
        requested_at=requested_at,
        max_age_seconds=1200,
        feed="iex",
    )
    tampered_payload = json.loads(valid.premarket_raw_payload_json)
    tampered_payload["bars"][0]["high"] = 99.0
    tampered_raw_json = json.dumps(
        tampered_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    tampered = replace(
        valid,
        premarket_raw_payload_json=tampered_raw_json,
        premarket_source_hash_sha256=hashlib.sha256(
            tampered_raw_json.encode("utf-8")
        ).hexdigest(),
    )
    monkeypatch.setattr(
        enrichment_service,
        "_observe_alpaca_tickers",
        lambda tickers, **kwargs: {ticker: tampered for ticker in tickers},
    )

    result = enrich_premarket_rows(
        [_row(source="alpaca_iex", preferred_source="alpaca_iex")],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=requested_at,
        source="alpaca",
        alpaca_provider=object(),
    )

    row = result["rows"][0]
    assert row["enrichment_status"] == "verified"
    assert row["enrichment_is_complete"] is True
    assert row["freshness_status"] == ""


def test_alpaca_observation_without_current_close_does_not_claim_freshness(
    monkeypatch,
):
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    valid = observation_from_alpaca_bars(
        "NOVA",
        [
            {
                "ticker": "NOVA",
                "timestamp": "2026-07-13T12:58:00Z",
                "high": 8.2,
                "low": 7.8,
                "close": 8.1,
                "volume": 1500,
            }
        ],
        previous_close=5.0,
        requested_at=requested_at,
        max_age_seconds=1200,
        feed="iex",
    )
    missing_close = replace(valid, latest_price=None)
    monkeypatch.setattr(
        enrichment_service,
        "_observe_alpaca_tickers",
        lambda tickers, **kwargs: {ticker: missing_close for ticker in tickers},
    )

    result = enrich_premarket_rows(
        [
            _row(
                source="alpaca_iex",
                preferred_source="alpaca_iex",
                premarket_price=8.0,
            )
        ],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=requested_at,
        source="alpaca",
        alpaca_provider=object(),
    )

    row = result["rows"][0]
    assert row["enrichment_status"] == "verified"
    assert row["enrichment_is_complete"] is True
    assert row["premarket_price"] == 8.0
    assert row["freshness_status"] == ""


def test_alpaca_primary_uses_explicit_provenance_labeled_yahoo_fallback():
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    yahoo_payload = _payload(
        timestamps=[_epoch(12, 55), _epoch(13, 0)],
        highs=[8.2, 8.4],
        lows=[7.8, 7.7],
        closes=[8.0, 8.1],
    )

    class PartialAlpacaProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return [
                {
                    "ticker": ticker,
                    "timestamp": "2026-07-13T12:55:00Z",
                    "high": 8.2,
                    "low": 7.8,
                    "close": 8.0,
                    "volume": 1000,
                }
                for ticker in symbols
                if ticker != "NOVA"
            ]

        def get_premarket_snapshot(self, symbols, config):
            return [SimpleNamespace(ticker=ticker, previous_close=5.0) for ticker in symbols]

    fallback_calls = []

    def fetcher(ticker, _config):
        fallback_calls.append(ticker)
        return yahoo_payload

    result = enrich_premarket_rows(
        [
            _row(ticker="NOVA"),
            _row(ticker="ALFA"),
            _row(ticker="BETA"),
            _row(ticker="GAMM"),
        ],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=requested_at,
        source="alpaca",
        alpaca_provider=PartialAlpacaProvider(),
        allow_yahoo_fallback=True,
        fetcher=fetcher,
    )
    rows = {row["ticker"]: row for row in result["rows"]}
    row = rows["NOVA"]

    assert fallback_calls == ["NOVA"]
    assert result["summary"]["source"] == "alpaca_market_data_iex"
    assert result["summary"]["secondary_source"] == "yahoo_finance_chart"
    assert result["summary"]["secondary_fallback_count"] == 1
    assert result["summary"]["secondary_fallback_ratio"] == 0.25
    assert result["summary"]["secondary_fallback_candidate_count"] == 1
    assert result["summary"]["secondary_fallback_candidate_ratio"] == 0.25
    assert result["summary"]["secondary_fallback_ceiling_ratio"] == 0.25
    assert result["summary"]["secondary_fallback_status"] == "applied"
    assert result["summary"]["selected_symbols"] == ["ALFA", "BETA", "GAMM", "NOVA"]
    assert result["summary"]["verified_by_source"] == {
        "alpaca_market_data_iex": 3,
        "yahoo_finance_chart": 1,
    }
    assert row["premarket_range_source"] == "yahoo_finance_chart"
    assert row["enrichment_status"] == "verified"
    assert row["enrichment_primary_source"] == "alpaca_market_data_iex"
    assert row["enrichment_fallback_status"] == "applied"
    assert row["enrichment_fallback_source"] == "yahoo_finance_chart"
    assert row["enrichment_was_fallback"] is True
    assert rows["ALFA"]["enrichment_fallback_status"] == "not_needed"


def test_alpaca_100_percent_symbol_fallback_attempts_research_only_yahoo():
    class EmptyAlpacaProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return []

        def get_premarket_snapshot(self, symbols, config):
            return [SimpleNamespace(ticker=ticker, previous_close=5.0) for ticker in symbols]

    yahoo_called = False

    def fetcher(_ticker, _config):
        nonlocal yahoo_called
        yahoo_called = True
        return {}

    result = enrich_premarket_rows(
        [_row()],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        source="alpaca",
        alpaca_provider=EmptyAlpacaProvider(),
        allow_yahoo_fallback=True,
        fetcher=fetcher,
    )

    assert yahoo_called is True
    assert result["summary"]["status"] == "unavailable"
    assert result["summary"]["secondary_fallback_status"] == "attempted_unusable"
    assert result["summary"]["selected_symbols"] == ["NOVA"]
    assert result["summary"]["ranking_eligible_count"] == 0
    assert result["summary"]["ranking_excluded_count"] == 1
    assert result["ranking_rows"] == []


def test_ranking_snapshot_contains_only_verified_selected_rows(tmp_path: Path):
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)

    class SparseAlpacaProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return [
                {
                    "ticker": "ALFA",
                    "timestamp": "2026-07-13T12:55:00Z",
                    "high": 8.2,
                    "low": 7.8,
                    "close": 8.0,
                    "volume": 1000,
                }
            ]

        def get_premarket_snapshot(self, symbols, config):
            return [SimpleNamespace(ticker=ticker, previous_close=5.0) for ticker in symbols]

    result = enrich_premarket_rows(
        [_row(ticker="ALFA"), _row(ticker="NOVA")],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=requested_at,
        source="alpaca",
        alpaca_provider=SparseAlpacaProvider(),
        allow_yahoo_fallback=True,
        out_dir=tmp_path,
    )

    with Path(result["paths"]["snapshot"]).open(encoding="utf-8", newline="") as handle:
        ranking_rows = list(csv.DictReader(handle))
    with Path(result["paths"]["all_rows_snapshot"]).open(
        encoding="utf-8", newline=""
    ) as handle:
        audit_rows = list(csv.DictReader(handle))

    assert [row["ticker"] for row in ranking_rows] == ["ALFA"]
    assert {row["ticker"] for row in audit_rows} == {"ALFA", "NOVA"}
    source_alfa = next(row for row in audit_rows if row["ticker"] == "ALFA")
    assert source_alfa["premarket_high"] == str(_row(ticker="ALFA")["premarket_high"])
    assert source_alfa["premarket_volume"] == str(
        _row(ticker="ALFA")["premarket_volume"]
    )
    assert source_alfa["premarket_range_source"] == ""

    ranked = SnapshotRow.from_mapping(ranking_rows[0])
    assert ranked.premarket_range_source == "alpaca_market_data_iex"
    assert ranked.premarket_range_source_url.endswith("/v2/stocks/bars")
    assert ranked.enrichment_status == "verified"
    assert ranked.enrichment_is_complete is True
    assert ranked.enrichment_bar_completed_at == "2026-07-13T12:56:00+00:00"
    assert len(ranked.enrichment_observation_sha256) == 64
    sources = field_sources(ranked)
    assert sources["premarket_high"] == "alpaca_market_data_iex"
    assert sources["premarket_low"] == "alpaca_market_data_iex"
    assert sources["premarket_volume"] == "alpaca_market_data_iex"
    assert sources["catalyst_headline"] == "missing"
    candidate = score_snapshot(ranked, ScannerConfig()).to_dict()
    lineage = candidate["source_lineage"]
    assert candidate["target_basis_source"].endswith("/v2/stocks/bars")
    assert json.loads(candidate["field_sources"])["premarket_high"] == (
        "alpaca_market_data_iex"
    )
    assert lineage["premarket_observation"]["source"] == (
        "alpaca_market_data_iex"
    )
    assert lineage["premarket_observation"]["bar_completed_at"] == (
        "2026-07-13T12:56:00+00:00"
    )
    assert lineage["premarket_observation"]["observation_sha256"] == (
        ranked.enrichment_observation_sha256
    )


def test_rehearsal_mode_never_fetches_market_data_and_stays_fixture_only():
    fetched = False

    def fetcher(_ticker, _config):
        nonlocal fetched
        fetched = True
        return {}

    rows = [_row(ticker="NOVA"), _row(ticker="ALFA")]
    for row in rows:
        row["fixture_only"] = True
        row["data_source_kind"] = "fixture"

    result = enrich_premarket_rows(
        rows,
        config=ScannerConfig(),
        requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        source="yahoo",
        fetcher=fetcher,
        rehearsal_mode=True,
    )

    assert fetched is False
    assert result["summary"]["status"] == "rehearsal_only"
    assert result["summary"]["ranking_policy"] == (
        "fixture_rehearsal_only_non_learning"
    )
    assert len(result["ranking_rows"]) == 2
    assert all(row["fixture_only"] is True for row in result["ranking_rows"])


def test_partial_alpaca_observation_preserves_exact_per_field_sources(tmp_path: Path):
    class PartialAlpacaProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return [
                {
                    "ticker": "NOVA",
                    "timestamp": "2026-07-13T12:55:00Z",
                    "high": 9.4,
                    "low": 8.8,
                    "volume": 1000,
                }
            ]

        def get_premarket_snapshot(self, symbols, config):
            return [SimpleNamespace(ticker="NOVA", previous_close=0.0)]

    source_row = _row(
        ticker="NOVA",
        source="stockanalysis_premarket",
        preferred_source="stockanalysis_premarket",
        premarket_price=9.1,
        previous_close=7.0,
        premarket_high=9.1,
        premarket_low=9.1,
        premarket_volume=500_000,
        gap_pct=30.0,
        dollar_volume=4_550_000,
    )
    result = enrich_premarket_rows(
        [source_row],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        source="alpaca",
        alpaca_provider=PartialAlpacaProvider(),
        out_dir=tmp_path,
    )

    with Path(result["paths"]["snapshot"]).open(encoding="utf-8", newline="") as handle:
        persisted = list(csv.DictReader(handle))
    assert len(persisted) == 1
    row = SnapshotRow.from_mapping(persisted[0])
    sources = field_sources(row)

    assert row.premarket_price == 9.1
    assert row.previous_close == 7.0
    assert row.gap_pct == 30.0
    assert row.premarket_high == 9.4
    assert row.premarket_low == 8.8
    assert row.premarket_volume == 1000
    assert row.dollar_volume == 9100.0
    assert sources["premarket_price"] == "stockanalysis_premarket"
    assert sources["previous_close"] == "stockanalysis_premarket"
    assert sources["gap_pct"] == "stockanalysis_premarket"
    assert sources["premarket_high"] == "alpaca_market_data_iex"
    assert sources["premarket_low"] == "alpaca_market_data_iex"
    assert sources["premarket_volume"] == "alpaca_market_data_iex"
    assert sources["dollar_volume"] == (
        "derived:stockanalysis_premarket+alpaca_market_data_iex"
    )


def test_alpaca_credential_failure_fails_closed_without_calling_yahoo():
    class CredentialFailureProvider:
        def validate_credentials(self):
            raise DataProviderError("Missing Alpaca credentials")

    yahoo_called = False

    def fetcher(_ticker, _config):
        nonlocal yahoo_called
        yahoo_called = True
        return {}

    with pytest.raises(DataProviderError, match="Systemic Alpaca premarket"):
        enrich_premarket_rows(
            [_row()],
            config=ScannerConfig(),
            requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
            source="alpaca",
            alpaca_provider=CredentialFailureProvider(),
            allow_yahoo_fallback=True,
            fetcher=fetcher,
        )

    assert yahoo_called is False


def test_alpaca_http_failure_fails_closed_without_calling_yahoo():
    class HttpFailureProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            raise DataProviderError("Alpaca market data request failed with HTTP 503")

    yahoo_called = False

    def fetcher(_ticker, _config):
        nonlocal yahoo_called
        yahoo_called = True
        return {}

    with pytest.raises(DataProviderError, match="Systemic Alpaca premarket"):
        enrich_premarket_rows(
            [_row()],
            config=ScannerConfig(),
            requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
            source="alpaca",
            alpaca_provider=HttpFailureProvider(),
            allow_yahoo_fallback=True,
            fetcher=fetcher,
        )

    assert yahoo_called is False


def test_alpaca_network_failure_fails_closed_without_calling_yahoo():
    class NetworkFailureProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            raise OSError("network unavailable")

    yahoo_called = False

    def fetcher(_ticker, _config):
        nonlocal yahoo_called
        yahoo_called = True
        return {}

    with pytest.raises(DataProviderError, match="Systemic Alpaca premarket"):
        enrich_premarket_rows(
            [_row()],
            config=ScannerConfig(),
            requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
            source="alpaca",
            alpaca_provider=NetworkFailureProvider(),
            allow_yahoo_fallback=True,
            fetcher=fetcher,
        )

    assert yahoo_called is False
