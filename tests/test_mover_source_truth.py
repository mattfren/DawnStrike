from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import intraday_scanner.providers.local_daily_movers_provider as local_provider_module
from intraday_scanner.alpha.risk_governor import evaluate_risk
from intraday_scanner.models import SnapshotRow
from intraday_scanner.mover_pattern_audit import (
    QuarantinedEvidenceError,
    _audit_mover_rows,
    assert_backfeed_not_quarantined,
    audit_retained_data,
)
from intraday_scanner.providers.daily_movers_base import normalize_daily_mover_rows
from intraday_scanner.providers.local_daily_movers_provider import (
    LocalDailyMoversProvider,
)
from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
)
from intraday_scanner.services import daily_movers_service
from intraday_scanner.services.daily_movers_service import (
    _local_rows_are_eligible,
    _providers,
    _public_web_collection_gate,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _web_config(*sources: WebSourceConfig) -> WebCollectionConfig:
    return WebCollectionConfig(
        enabled=True,
        respect_robots=True,
        user_agent="DawnstrikeTest/1.0",
        timeout_seconds=1.0,
        rate_limit_seconds=0.0,
        save_raw=False,
        allowed_domains=("stockanalysis.com", "tradingview.com"),
        sources=tuple(sources),
    )


def test_daily_movers_never_reuses_premarket_sources() -> None:
    config = _web_config(
        WebSourceConfig(
            name="stockanalysis_premarket",
            type="stockanalysis_daily_movers",
            url="https://stockanalysis.com/markets/premarket/",
        ),
        WebSourceConfig(
            name="tradingview_premarket",
            type="tradingview_daily_movers",
            url=(
                "https://www.tradingview.com/markets/stocks-usa/"
                "market-movers-pre-market-gainers/"
            ),
        ),
    )

    providers = _providers("2026-07-16", config)
    web_urls = [
        provider.source.url
        for name, provider in providers
        if name != "local_daily_movers"
    ]

    assert web_urls
    assert all("premarket" not in url.lower() for url in web_urls)
    assert all("pre-market" not in url.lower() for url in web_urls)
    assert "https://stockanalysis.com/markets/gainers/" in web_urls


def test_explicit_daily_mover_sources_are_preserved() -> None:
    expected = "https://stockanalysis.com/markets/gainers/"
    config = _web_config(
        WebSourceConfig(
            name="stockanalysis_eod_gainers",
            type="stockanalysis_daily_movers",
            url=expected,
        )
    )

    providers = _providers("2026-07-16", config)
    web_urls = [
        provider.source.url
        for name, provider in providers
        if name.startswith("stockanalysis_")
    ]

    assert web_urls == [expected]


@pytest.mark.parametrize(
    ("market_date", "as_of", "expected", "reason"),
    [
        (
            "2026-07-16",
            datetime(2026, 7, 16, 19, 59, tzinfo=timezone.utc),
            False,
            "public_gainers_unavailable_before_official_exchange_close",
        ),
        (
            "2026-07-16",
            datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc),
            True,
            "current_published_session_after_official_exchange_close",
        ),
        (
            "2026-07-15",
            datetime(2026, 7, 16, 21, 0, tzinfo=timezone.utc),
            False,
            "public_gainers_requires_current_market_date",
        ),
        (
            "2026-07-03",
            datetime(2026, 7, 3, 21, 0, tzinfo=timezone.utc),
            False,
            "public_gainers_requires_published_trading_session",
        ),
        (
            "2026-07-04",
            datetime(2026, 7, 4, 21, 0, tzinfo=timezone.utc),
            False,
            "public_gainers_requires_published_trading_session",
        ),
        (
            "2026-11-27",
            datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc),
            False,
            "public_gainers_unavailable_before_official_exchange_close",
        ),
        (
            "2026-11-27",
            datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc),
            True,
            "current_published_session_after_official_exchange_close",
        ),
    ],
)
def test_public_gainers_gate_requires_current_published_session_after_close(
    market_date: str,
    as_of: datetime,
    expected: bool,
    reason: str,
) -> None:
    result = _public_web_collection_gate(market_date, as_of=as_of)

    assert result["eligible"] is expected
    assert result["reason"] == reason
    assert result["dataset_role"] == "descriptive_current_session_gainers"
    assert result["prospective_signal_eligible"] is False
    assert result["source_snapshot_kind"] == "current_session_public_gainers"
    assert result["source_coverage_complete"] is False


def test_public_gainers_gate_rejects_naive_clock() -> None:
    with pytest.raises(ValueError, match="timestamps must include a timezone"):
        _public_web_collection_gate(
            "2026-07-16",
            as_of=datetime(2026, 7, 16, 20, 0),
        )


class _FakeProvider:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls = 0

    def collect(self, *, market_date: str, out_dir: object) -> dict[str, Any]:
        _ = market_date, out_dir
        self.calls += 1
        return self.result


def _normalized_mover(market_date: str, *, source: str) -> dict[str, Any]:
    rows, rejected, _counts = normalize_daily_mover_rows(
        [{"ticker": "MOVE", "rank": 1, "price": 12.0, "change_pct": 20.0}],
        market_date=market_date,
        source=source,
        source_url="https://example.test/gainers/",
        extracted_at=f"{market_date}T20:01:00+00:00",
    )
    assert not rejected
    return rows[0]


def _write_verified_local_movers(
    path: Path,
    *,
    market_date: str,
    extracted_at: str,
    coverage_complete: str = "true",
    corporate_action_status: str = "verified_clear",
    corporate_action_observed_at: str | None = None,
) -> None:
    corporate_action_path = path.with_suffix(".corporate_actions.json")
    corporate_action_payload = {
        "schema_version": "v2.corporate_action_evidence.v1",
        "market_date": market_date,
        "symbol": "MOVE",
        "corporate_action_status": corporate_action_status,
        "source": "test_exchange_action_feed",
        "observed_at": corporate_action_observed_at or extracted_at,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    corporate_action_bytes = json.dumps(
        corporate_action_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    corporate_action_path.parent.mkdir(parents=True, exist_ok=True)
    corporate_action_path.write_bytes(corporate_action_bytes)
    corporate_action_ref = (
        "sha256:" + hashlib.sha256(corporate_action_bytes).hexdigest()
    )
    rows = [
        {
            "market_date": market_date,
            "ticker": "MOVE",
            "company": "Mover Inc",
            "rank": 1,
            "price": 12.0,
            "change_pct": 20.0,
            "volume": 100_000,
            "extracted_at": extracted_at,
            "source_coverage_complete": coverage_complete,
            "list_coverage_complete": coverage_complete,
            "expected_row_count": 1,
            "corporate_action_status": corporate_action_status,
            "corporate_action_source_ref": corporate_action_ref,
            "corporate_action_source_path": str(corporate_action_path.resolve()),
        }
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _audited_review_fixture(
    tmp_path: Path,
) -> tuple[SQLiteScanStore, Path, Path]:
    source_path = tmp_path / "eligible_daily_movers.csv"
    _write_verified_local_movers(
        source_path,
        market_date="2026-07-16",
        extracted_at="2026-07-16T20:01:00+00:00",
    )
    provider = LocalDailyMoversProvider(source_path).collect(
        market_date="2026-07-16",
        out_dir=tmp_path / "provider",
    )
    assert provider["status"] == "success", provider
    db_path = tmp_path / "review.sqlite3"
    store = SQLiteScanStore(db_path)
    store.persist_daily_market_movers(
        provider["rows"],
        market_date="2026-07-16",
    )
    store.persist_daily_review(
        {"review_id": "review-clear", "market_date": "2026-07-16"},
        [],
        [],
    )
    store.persist_daily_review(
        {"review_id": "review-blocked", "market_date": "2026-07-15"},
        [],
        [],
    )
    audit = audit_retained_data(
        db_path=db_path,
        output_root=tmp_path / "audit",
    )
    return store, db_path, Path(audit["quarantine_manifest_path"])


def test_open_session_skips_public_and_does_not_erase_prior_persisted_rows(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "movers.sqlite"
    store = SQLiteScanStore(db_path)
    store.initialize()
    store.persist_daily_market_movers(
        [_normalized_mover("2026-07-16", source="prior_valid_close")],
        market_date="2026-07-16",
        replace=True,
    )
    local = _FakeProvider(
        {
            "status": "missing",
            "source": "local_daily_movers",
            "failure_reason": "local daily movers CSV not found",
            "rows": [],
            "rejected_rows": [],
        }
    )
    include_public: list[bool] = []

    monkeypatch.setattr(
        daily_movers_service,
        "load_web_sources_config",
        lambda _path: _web_config(),
    )

    def providers(_date, _config, *, include_public_web=True):
        include_public.append(include_public_web)
        return [("local_daily_movers", local)]

    monkeypatch.setattr(daily_movers_service, "_providers", providers)

    result = daily_movers_service.collect_daily_movers(
        market_date="2026-07-16",
        db_path=db_path,
        out_dir=tmp_path / "out",
        persist=True,
        as_of=datetime(2026, 7, 16, 19, 59, tzinfo=timezone.utc),
    )

    assert result["status"] == "no_data"
    assert include_public == [False]
    assert local.calls == 1
    retained = store.load_daily_market_movers(market_date="2026-07-16")
    assert len(retained) == 1
    assert retained[0]["source"] == "prior_valid_close"


def test_after_close_public_gainers_persist_with_descriptive_session_truth(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "movers.sqlite"
    local = _FakeProvider(
        {
            "status": "missing",
            "source": "local_daily_movers",
            "failure_reason": "local daily movers CSV not found",
            "rows": [],
            "rejected_rows": [],
        }
    )
    public = _FakeProvider(
        {
            "status": "success",
            "source": "stockanalysis_daily_movers",
            "rows": [
                _normalized_mover(
                    "2026-07-16",
                    source="stockanalysis_daily_movers",
                )
            ],
            "rejected_rows": [],
        }
    )

    monkeypatch.setattr(
        daily_movers_service,
        "load_web_sources_config",
        lambda _path: _web_config(),
    )

    def providers(_date, _config, *, include_public_web=True):
        assert include_public_web is True
        return [
            ("local_daily_movers", local),
            ("stockanalysis_1", public),
        ]

    monkeypatch.setattr(daily_movers_service, "_providers", providers)

    result = daily_movers_service.collect_daily_movers(
        market_date="2026-07-16",
        db_path=db_path,
        out_dir=tmp_path / "out",
        persist=True,
        as_of=datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "success"
    assert result["public_web_gate"]["eligible"] is True
    assert public.calls == 1
    persisted = SQLiteScanStore(db_path).load_daily_market_movers(
        market_date="2026-07-16"
    )
    assert len(persisted) == 1
    assert persisted[0]["dataset_role"] == "descriptive_current_session_gainers"
    assert persisted[0]["prospective_signal_eligible"] is False
    assert persisted[0]["source_snapshot_kind"] == "current_session_public_gainers"
    assert persisted[0]["source_coverage_complete"] is False
    assert persisted[0]["source_complete"] is False
    assert persisted[0]["list_coverage_complete"] is False
    assert persisted[0]["corporate_action_status"] == "unverified"
    assert persisted[0]["eod_label_eligible"] is False
    assert persisted[0]["ingestion_channel"] == "public_web_current_session_gainers"
    assert persisted[0]["public_web_session_gate"]["eligible"] is True
    audited = _audit_mover_rows(persisted)
    assert audited[0]["eligible"] is False
    assert "missing_descriptive_eod_role" in audited[0]["reasons"]
    assert "source_coverage_not_proven_complete" in audited[0]["reasons"]


def test_historical_local_import_remains_allowed_and_descriptive_only(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "movers.sqlite"
    local_path = tmp_path / "daily_movers_2026-07-16.csv"
    _write_verified_local_movers(
        local_path,
        market_date="2026-07-16",
        extracted_at="2026-07-16T20:01:00+00:00",
    )
    local = LocalDailyMoversProvider(local_path)

    monkeypatch.setattr(
        daily_movers_service,
        "load_web_sources_config",
        lambda _path: _web_config(),
    )

    def providers(_date, _config, *, include_public_web=True):
        assert include_public_web is False
        return [("local_daily_movers", local)]

    monkeypatch.setattr(daily_movers_service, "_providers", providers)

    result = daily_movers_service.collect_daily_movers(
        market_date="2026-07-16",
        db_path=db_path,
        out_dir=tmp_path / "out",
        persist=True,
        as_of=datetime(2026, 7, 17, 21, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "success"
    assert result["public_web_gate"]["eligible"] is False
    persisted = SQLiteScanStore(db_path).load_daily_market_movers(
        market_date="2026-07-16"
    )
    assert len(persisted) == 1
    assert persisted[0]["dataset_role"] == "descriptive_eod_movers"
    assert persisted[0]["prospective_signal_eligible"] is False
    assert persisted[0]["source_snapshot_kind"] == "realized_eod_gainers"
    assert persisted[0]["source_coverage_complete"] is True
    assert persisted[0]["source_complete"] is True
    assert persisted[0]["list_coverage_complete"] is True
    assert persisted[0]["expected_row_count"] == 1
    assert persisted[0]["corporate_action_status"] == "verified_clear"
    assert persisted[0]["eod_label_eligible"] is True
    assert persisted[0]["source_ref"] == persisted[0]["source_artifact_ref"]
    assert persisted[0]["source_artifact_ref"].startswith("sha256:")
    assert persisted[0]["ingestion_channel"] == "local_operator_csv"
    assert "public_web_session_gate" not in persisted[0]
    audited = _audit_mover_rows(persisted)
    assert audited[0]["eligible"] is True, audited[0]
    assert audited[0]["reasons"] == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"market_date": ""}, "every_row_requires_explicit_market_date"),
        ({"market_date": "2026-07-15"}, "row_market_date_mismatch"),
        (
            {"extracted_at": "2026-07-16T19:59:00+00:00"},
            "extracted_at_must_be_at_or_after_official_close",
        ),
        (
            {"coverage_complete": "false"},
            "source_coverage_complete_must_be_explicit_true",
        ),
        (
            {"corporate_action_status": "unverified"},
            "corporate_action_status_must_be_verified_or_adjusted",
        ),
    ],
)
def test_local_eod_import_fails_closed_without_complete_truth(
    tmp_path: Path,
    overrides: dict[str, str],
    reason: str,
) -> None:
    path = tmp_path / "local.csv"
    _write_verified_local_movers(
        path,
        market_date=overrides.get("market_date", "2026-07-16"),
        extracted_at=overrides.get(
            "extracted_at", "2026-07-16T20:01:00+00:00"
        ),
        coverage_complete=overrides.get("coverage_complete", "true"),
        corporate_action_status=overrides.get(
            "corporate_action_status", "verified_clear"
        ),
    )

    result = LocalDailyMoversProvider(path).collect(
        market_date="2026-07-16",
        out_dir=tmp_path / "out",
    )

    assert result["status"] == "ineligible_source_truth"
    assert reason in result["truth_gate"]["reasons"]
    assert result["rows"]
    assert all(row["source_coverage_complete"] is False for row in result["rows"])
    assert all(row["eod_label_eligible"] is False for row in result["rows"])


def test_local_eod_rejects_declared_capture_after_system_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "future_capture.csv"
    _write_verified_local_movers(
        path,
        market_date="2026-07-16",
        extracted_at="2026-07-16T20:01:00+00:00",
    )
    monkeypatch.setattr(
        local_provider_module,
        "_utc_now",
        lambda: datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc),
    )

    result = LocalDailyMoversProvider(path).collect(
        market_date="2026-07-16",
        out_dir=tmp_path / "out",
    )

    assert result["status"] == "ineligible_source_truth"
    assert "extracted_at_cannot_be_after_system_receipt" in result["truth_gate"][
        "reasons"
    ]


def test_local_eod_rejects_missing_independent_corporate_action_artifact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing_action_source.csv"
    _write_verified_local_movers(
        path,
        market_date="2026-07-16",
        extracted_at="2026-07-16T20:01:00+00:00",
    )
    path.with_suffix(".corporate_actions.json").unlink()

    result = LocalDailyMoversProvider(path).collect(
        market_date="2026-07-16",
        out_dir=tmp_path / "out",
    )

    assert result["status"] == "ineligible_source_truth"
    assert any(
        reason.endswith("corporate_action_source_hash_invalid")
        for reason in result["truth_gate"]["reasons"]
    )


def test_local_eod_rejects_prior_day_corporate_action_observation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale_action_source.csv"
    _write_verified_local_movers(
        path,
        market_date="2026-07-16",
        extracted_at="2026-07-16T20:01:00+00:00",
        corporate_action_observed_at="2026-07-15T20:01:00+00:00",
    )

    result = LocalDailyMoversProvider(path).collect(
        market_date="2026-07-16",
        out_dir=tmp_path / "out",
    )

    assert result["status"] == "ineligible_source_truth"
    assert (
        "corporate_action_source_artifact_missing_or_hash_invalid"
        in result["truth_gate"]["reasons"]
    )


def test_service_rejects_local_rows_after_retained_artifact_is_tampered(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local.csv"
    _write_verified_local_movers(
        path,
        market_date="2026-07-16",
        extracted_at="2026-07-16T20:01:00+00:00",
    )
    result = LocalDailyMoversProvider(path).collect(
        market_date="2026-07-16",
        out_dir=tmp_path / "out",
    )
    assert result["status"] == "success"
    retained_path = Path(result["source_artifact_path"])
    retained_path.write_text(
        retained_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    assert not _local_rows_are_eligible(
        result["rows"],
        market_date="2026-07-16",
    )


def test_quarantine_guard_blocks_listed_review_and_allows_proven_clear_id(
    tmp_path: Path,
) -> None:
    store, db_path, path = _audited_review_fixture(tmp_path)

    assert_backfeed_not_quarantined("review-clear", path, db_path=db_path)
    with pytest.raises(QuarantinedEvidenceError, match="is quarantined"):
        assert_backfeed_not_quarantined(
            "review-blocked", path, db_path=db_path
        )
    with pytest.raises(QuarantinedEvidenceError, match="not positively cleared"):
        assert_backfeed_not_quarantined(
            "review-new-after-audit", path, db_path=db_path
        )

    store.persist_daily_review(
        {"review_id": "review-after-audit", "market_date": "2026-07-14"},
        [],
        [],
    )
    with pytest.raises(QuarantinedEvidenceError, match="stale"):
        assert_backfeed_not_quarantined(
            "review-clear", path, db_path=db_path
        )


@pytest.mark.parametrize(
    "override",
    [
        {"schema_version": "v1.unknown"},
        {"status": "ready"},
        {"database_mutated": True},
        {"automatic_application_allowed": True},
        {"automatic_application_allowed": None},
        {"learning_eligible": True},
        {"review_ids": "not-a-list"},
        {"eligible_review_ids": "not-a-list"},
        {"audit_input_fingerprint": "sha256:not-a-digest"},
    ],
)
def test_quarantine_guard_fails_closed_for_invalid_manifest(
    tmp_path: Path,
    override: dict[str, object],
) -> None:
    _store, db_path, valid_path = _audited_review_fixture(tmp_path)
    payload: dict[str, object] = json.loads(
        valid_path.read_text(encoding="utf-8")
    )
    payload.update(override)
    path = tmp_path / "quarantine.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(QuarantinedEvidenceError):
        assert_backfeed_not_quarantined(
            "review-clear", path, db_path=db_path
        )


def test_quarantine_guard_fails_closed_when_manifest_is_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(QuarantinedEvidenceError, match="unavailable or invalid"):
        assert_backfeed_not_quarantined(
            "review-clear",
            tmp_path / "missing.json",
            db_path=tmp_path / "missing.sqlite3",
        )


def test_applied_backfeed_consumer_requires_current_audit_receipt(
    tmp_path: Path,
) -> None:
    store, _db_path, manifest_path = _audited_review_fixture(tmp_path)
    event = {
        "event_id": "event-1",
        "review_id": "review-clear",
        "market_date": "2026-07-16",
        "event_type": "paper_research_adjustment",
        "target": "candidate_threshold",
        "confidence": 0.75,
        "sample_size": 20,
        "applied": True,
    }

    result = store.replace_learning_backfeed_events(
        review_id="review-clear",
        events=[event],
        quarantine_manifest_path=manifest_path,
    )
    assert result["inserted"] == 1

    with pytest.raises(QuarantinedEvidenceError, match="stale"):
        store.replace_learning_backfeed_events(
            review_id="review-clear",
            events=[{**event, "event_id": "event-2"}],
            quarantine_manifest_path=manifest_path,
        )


def test_provider_relative_volume_survives_snapshot_normalization() -> None:
    row = {
        "ticker": "MOVE",
        "company": "Mover",
        "previous_close": 10,
        "premarket_price": 11,
        "premarket_high": 11.2,
        "premarket_low": 10.8,
        "premarket_volume": 250_000,
        "relative_volume": 3.75,
        "dollar_volume": 2_750_000,
        "gap_pct": 10,
        "float_shares": 5_000_000,
        "market_cap": 55_000_000,
        "spread_pct": 0.5,
        "short_float_pct": 4,
        "has_news": True,
        "catalyst_headline": "Timestamped test catalyst",
        "catalyst_url": "https://example.test/catalyst",
        "current_halt": False,
        "recent_offering": False,
        "reverse_split_90d": False,
        "source": "fixture",
        "as_of_timestamp": "2026-07-16T13:00:00+00:00",
    }

    snapshot = SnapshotRow.from_mapping(row)

    assert snapshot.relative_volume == 3.75
    assert snapshot.to_dict()["relative_volume"] == 3.75


def test_recent_reverse_split_is_a_hard_alert_veto() -> None:
    decision = evaluate_risk(
        {
            "ticker": "SPLT",
            "premarket_price": 5,
            "premarket_volume": 500_000,
            "source_confidence": 90,
            "reverse_split_90d": True,
        }
    )

    assert decision.can_alert is False
    assert "reverse_split_90d" in decision.hard_avoid_reasons
