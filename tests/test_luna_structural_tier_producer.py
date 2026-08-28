"""Focused adversarial coverage for the Luna structural/Tier producers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import intraday_scanner.services.luna_research_slate_service as luna_slate_module
import intraday_scanner.services.price_observation_service as price_observation_module
import intraday_scanner.services.trade_watcher_service as trade_watcher_module
from intraday_scanner.alpha.episode_identity import build_episode_identity
from intraday_scanner.alpha.plan_constructor import NO_VALID_PLAN, construct_alphaops_v5_plan
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_STRATEGY_VERSION,
    evaluate_v5_official_paper,
)
from intraday_scanner.config import ScannerConfig, load_config
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SnapshotRow
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.scoring import score_snapshot
from intraday_scanner.services.alpha_cycle_service import (
    _attach_authenticated_alpaca_structure,
    _build_modeled_cost_receipt,
    _signal_payload,
)
from intraday_scanner.services.luna_research_slate_service import (
    TIER1,
    _valid_modeled_cost_receipt,
    apply_publication_semantics,
    build_ranked_research_slate,
    validate_watcher_current_proof,
)
from intraday_scanner.services.premarket_enrichment_service import (
    observation_from_alpaca_bars,
)
from intraday_scanner.services.trade_watcher_service import (
    ACTION_ENTER_SHORT,
    ACTION_EXIT_SHORT,
    WatcherSettings,
    _build_watcher_current_proof,
    _close_paper_position,
    _decision_for_signal,
    _entry_decision,
    _monitor_publication_receipt,
    _open_paper_position,
    _side_aware_quote_observation,
    run_trade_watcher,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _production_row() -> dict[str, object]:
    raw = {
        "ticker": "NOVA",
        "timestamp": "2026-08-25T00:00:00+00:00",
        "high": 12.0,
        "bar": {"t": "2026-08-25T00:00:00Z", "h": 12.0},
    }
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    observation = {
        "ticker": "NOVA",
        "status": "verified",
        "premarket_high": 10.0,
        "premarket_low": 9.0,
        "previous_close": 8.0,
        "latest_price": 9.5,
        "premarket_volume": 1000,
        "observed_at": "2026-08-26T13:00:00+00:00",
        "bar_completed_at": "2026-08-26T13:01:00+00:00",
        "is_complete": True,
        "bar_count": 1,
        "age_seconds": 30,
        "source": "alpaca_market_data_iex",
        "source_url": "https://data.alpaca.markets/v2/stocks/bars",
        "failure_reason": "",
        "prior_daily_high": 12.0,
        "prior_daily_high_observed_at": "2026-08-25T00:00:00+00:00",
        "prior_daily_high_completed_at": "2026-08-26T00:00:00+00:00",
        "prior_daily_high_completion_semantics": "availability_boundary",
        "prior_daily_high_source": "alpaca_market_data_iex",
        "prior_daily_high_source_url": "https://data.alpaca.markets/v2/stocks/bars",
        "prior_daily_high_source_hash": hashlib.sha256(raw_json.encode()).hexdigest(),
        "prior_daily_high_raw_payload_json": raw_json,
    }
    observation_json = json.dumps(observation, sort_keys=True, separators=(",", ":"))
    premarket_raw = {
        "ticker": "NOVA",
        "feed": "iex",
        "requested_at": "2026-08-26T13:30:00+00:00",
        "bars": [
            {
                "ticker": "NOVA",
                "timestamp": "2026-08-26T13:00:00+00:00",
                "high": 10.0,
                "low": 9.0,
                "close": 9.5,
                "volume": 1000,
            }
        ],
    }
    premarket_raw_json = json.dumps(premarket_raw, sort_keys=True, separators=(",", ":"))
    return {
        "ticker": "NOVA",
        "strategy_id": "alphaops_v5",
        "premarket_high": 10.0,
        "premarket_low": 9.0,
        "premarket_range_source": "alpaca_market_data_iex",
        "premarket_range_source_url": "https://data.alpaca.markets/v2/stocks/bars",
        "enrichment_primary_source": "alpaca_market_data_iex",
        "enrichment_status": "verified",
        "enrichment_is_complete": True,
        "enrichment_was_fallback": False,
        "enrichment_observed_at": "2026-08-26T13:00:00+00:00",
        "enrichment_bar_completed_at": "2026-08-26T13:01:00+00:00",
        "enrichment_observation_sha256": hashlib.sha256(observation_json.encode()).hexdigest(),
        "enrichment_observation_payload_json": observation_json,
        "premarket_raw_payload_json": premarket_raw_json,
        "premarket_source_hash_sha256": hashlib.sha256(premarket_raw_json.encode()).hexdigest(),
        "prior_daily_high": 12.0,
        "prior_daily_high_observed_at": "2026-08-25T00:00:00+00:00",
        "prior_daily_high_completed_at": "2026-08-26T00:00:00+00:00",
        "prior_daily_high_completion_semantics": "availability_boundary",
        "prior_daily_high_source": "alpaca_market_data_iex",
        "prior_daily_high_source_url": "https://data.alpaca.markets/v2/stocks/bars",
        "prior_daily_high_source_hash": hashlib.sha256(raw_json.encode()).hexdigest(),
        "prior_daily_high_raw_payload_json": raw_json,
    }


@pytest.fixture(autouse=True)
def _freeze_watcher_validator_clock(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat("2026-08-26T13:30:00+00:00")
            return value.astimezone(tz) if tz is not None else value

    monkeypatch.setattr(luna_slate_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        trade_watcher_module,
        "_utc_now",
        lambda: "2026-08-26T13:30:00+00:00",
    )


def test_authenticated_production_plan_uses_prior_daily_high_only() -> None:
    payload = _signal_payload(
        _attach_authenticated_alpaca_structure(
            _production_row(), decision_at="2026-08-26T13:30:00+00:00"
        ),
        "scan",
        "2026-08-26T13:30:00+00:00",
        1,
    )
    assert payload["plan_construction_status"] == "COMPLETE"
    assert payload["target_1"] == 12.0
    assert payload["target_basis_kind"] == "prior_day_resistance"


def test_scored_candidate_preserves_authenticated_structure_evidence_end_to_end() -> None:
    production = _production_row()
    snapshot = SnapshotRow.from_mapping(
        {
            **production,
            "company": "Nova",
            "premarket_price": 9.5,
            "previous_close": 8.0,
            "premarket_volume": 1_000,
            "dollar_volume": 9_500,
            "gap_pct": 18.75,
            "float_shares": 10_000_000,
            "market_cap": 95_000_000,
            "spread_pct": 0.5,
            "short_float_pct": 5.0,
            "has_news": True,
            "catalyst_headline": "Authenticated catalyst",
            "catalyst_url": "https://example.test/catalyst",
            "current_halt": False,
            "recent_offering": False,
            "reverse_split_90d": False,
            "source": "alpaca_iex",
            "as_of_timestamp": "2026-08-26T13:00:00+00:00",
        }
    )

    scored = score_snapshot(snapshot, ScannerConfig()).to_dict()
    for field in (
        "enrichment_observation_payload_json",
        "premarket_raw_payload_json",
        "premarket_source_hash_sha256",
        "prior_daily_high",
        "prior_daily_high_raw_payload_json",
        "prior_daily_high_source_hash",
    ):
        assert scored[field] == production[field]

    structured = _signal_payload(
        _attach_authenticated_alpaca_structure(scored, decision_at="2026-08-26T13:30:00+00:00"),
        "scan",
        "2026-08-26T13:30:00+00:00",
        1,
    )
    assert structured["plan_construction_status"] == "COMPLETE"
    assert structured["target_1"] == 12.0


def test_target_raw_artifact_mutation_fails_closed() -> None:
    row = _production_row()
    row["prior_daily_high_raw_payload_json"] = json.dumps(
        {
            "ticker": "NOVA",
            "timestamp": "2026-08-25T00:00:00+00:00",
            "high": 99.0,
            "bar": {"t": "2026-08-25T00:00:00Z", "h": 99.0},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    enriched = _attach_authenticated_alpaca_structure(row, decision_at="2026-08-26T13:30:00+00:00")
    assert "market_structure_observations" not in enriched


def test_target_outer_value_mutation_fails_even_when_digest_replays() -> None:
    row = _production_row()
    row["prior_daily_high"] = 99.0
    assert "market_structure_observations" not in _attach_authenticated_alpaca_structure(
        row, decision_at="2026-08-26T13:30:00+00:00"
    )


def test_premarket_aggregate_rehash_cannot_override_raw_bars() -> None:
    row = _production_row()
    payload = json.loads(row["enrichment_observation_payload_json"])
    payload["premarket_high"] = 99.0
    row["premarket_high"] = 99.0
    row["enrichment_observation_payload_json"] = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    row["enrichment_observation_sha256"] = hashlib.sha256(
        row["enrichment_observation_payload_json"].encode()
    ).hexdigest()
    assert "market_structure_observations" not in _attach_authenticated_alpaca_structure(
        row, decision_at="2026-08-26T13:30:00+00:00"
    )


@pytest.mark.parametrize(
    ("mutation",),
    [
        (lambda raw: raw["bars"].__setitem__(0, {**raw["bars"][0], "ticker": "OTHER"}),),
        (
            lambda raw: raw["bars"].__setitem__(
                0, {**raw["bars"][0], "timestamp": "2026-08-26T13:31:00+00:00"}
            ),
        ),
        (
            lambda raw: raw["bars"].__setitem__(
                0, {**raw["bars"][0], "timestamp": "2026-08-26T03:00:00+00:00"}
            ),
        ),
        (lambda raw: raw.update({"requested_at": "2026-08-26T13:29:00+00:00"}),),
    ],
)
def test_rehashed_premarket_raw_artifact_rejects_invalid_session_or_lineage(mutation) -> None:
    row = _production_row()
    raw = json.loads(row["premarket_raw_payload_json"])
    mutation(raw)
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    row["premarket_raw_payload_json"] = raw_json
    row["premarket_source_hash_sha256"] = hashlib.sha256(raw_json.encode()).hexdigest()
    assert "market_structure_observations" not in _attach_authenticated_alpaca_structure(
        row, decision_at="2026-08-26T13:30:00+00:00"
    )


def test_rehashed_premarket_raw_reordered_bars_rejected() -> None:
    row = _production_row()
    raw = json.loads(row["premarket_raw_payload_json"])
    raw["bars"] = [
        {**raw["bars"][0], "timestamp": "2026-08-26T13:01:00+00:00"},
        raw["bars"][0],
    ]
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    row["premarket_raw_payload_json"] = raw_json
    row["premarket_source_hash_sha256"] = hashlib.sha256(raw_json.encode()).hexdigest()
    assert "market_structure_observations" not in _attach_authenticated_alpaca_structure(
        row, decision_at="2026-08-26T13:30:00+00:00"
    )


def test_rehashed_premarket_raw_aggregate_time_mismatch_rejected() -> None:
    row = _production_row()
    raw = json.loads(row["premarket_raw_payload_json"])
    raw["bars"][0]["timestamp"] = "2026-08-26T12:59:00+00:00"
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    row["premarket_raw_payload_json"] = raw_json
    row["premarket_source_hash_sha256"] = hashlib.sha256(raw_json.encode()).hexdigest()
    assert "market_structure_observations" not in _attach_authenticated_alpaca_structure(
        row, decision_at="2026-08-26T13:30:00+00:00"
    )


def test_premarket_observation_persists_prior_artifact_contract() -> None:
    raw = {
        "ticker": "NOVA",
        "timestamp": "2026-08-25T00:00:00+00:00",
        "high": 12.0,
        "bar": {"t": "2026-08-25T00:00:00Z", "h": 12.0},
    }
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    observation = observation_from_alpaca_bars(
        "NOVA",
        [{"timestamp": "2026-08-26T12:59:00Z", "high": 10.0, "low": 9.0, "close": 9.5}],
        previous_close=8.0,
        requested_at=datetime.fromisoformat("2026-08-26T13:00:00+00:00"),
        max_age_seconds=1200,
        feed="iex",
        prior_daily_high={
            "ticker": "NOVA",
            "high": 12.0,
            "observed_at": raw["timestamp"],
            "completed_at": "2026-08-26T13:00:00+00:00",
            "completion_semantics": "availability_boundary",
            "source": "alpaca_market_data_iex",
            "source_url": "https://data.alpaca.markets/v2/stocks/bars",
            "source_hash": hashlib.sha256(raw_json.encode()).hexdigest(),
            "raw_payload_json": raw_json,
        },
    )
    assert observation.has_prior_daily_high
    assert observation.prior_daily_high_raw_payload_json == raw_json
    assert observation.prior_daily_high_completion_semantics == "availability_boundary"


def test_missing_target_is_no_valid_plan() -> None:
    row = _production_row()
    for key in tuple(row):
        if key.startswith("prior_daily_high"):
            row[key] = "" if isinstance(row[key], str) else None
    payload = _signal_payload(
        _attach_authenticated_alpaca_structure(row, decision_at="2026-08-26T13:30:00+00:00"),
        "scan",
        "2026-08-26T13:30:00+00:00",
        1,
    )
    assert payload["alphaops_market_structure_plan"]["status"] == NO_VALID_PLAN


def test_alpha_cycle_reuses_one_microsecond_decision_timestamp_end_to_end(
    monkeypatch, tmp_path
) -> None:
    """Exercise the production cycle seam, not just the plan helper.

    The scorer historically emitted a second-rounded timestamp while
    premarket enrichment retained the caller's microseconds.  This fixture
    drives the real ``alpha_cycle`` orchestration with external collectors
    and notification side effects stubbed, then reads the persisted signal.
    """

    import intraday_scanner.services.alpha_cycle_service as alpha_cycle_module

    candidate, _ = _watcher_signal()
    candidate = dict(candidate)
    # Keep the fake source lane eligible for the real frozen-slate producer.
    # These are the same explicit safety fields required of production rows;
    # without them a legitimate signal is intentionally omitted from the
    # immutable research cohort.
    candidate.update(
        {
            "freshness_status": "FRESH",
            "input_status": "READY",
            "evidence_status": "COMPLETE",
            "universe_lane": "mover",
            "evidence_lane": "mover",
        }
    )
    # Let the real cycle generate the canonical signal key used by the
    # historical ledger and frozen selection below.
    for preexisting_selection_field in (
        "signal_id",
        "selection_id",
        "cohort",
        "frozen_ranked_research_slate",
        "frozen_slate_lineage",
        "source_scan_id",
    ):
        candidate.pop(preexisting_selection_field, None)
    cycle_at = datetime(2026, 8, 26, 13, 30, 0, 123456, tzinfo=timezone.utc)
    cycle_timestamp = cycle_at.isoformat()
    # The authenticated premarket receipt is requested at this exact cycle
    # boundary.  Rebind the fixture's raw receipt before the real cycle seam
    # runs so a microsecond decision timestamp is causally valid rather than
    # being mistaken for the historical second-rounded scan timestamp.
    premarket_raw = json.loads(candidate["premarket_raw_payload_json"])
    premarket_raw["requested_at"] = cycle_timestamp
    premarket_raw["bars"][0]["timestamp"] = "2026-08-26T13:28:00+00:00"
    premarket_raw_json = json.dumps(premarket_raw, sort_keys=True, separators=(",", ":"))
    candidate["premarket_raw_payload_json"] = premarket_raw_json
    candidate["premarket_source_hash_sha256"] = hashlib.sha256(
        premarket_raw_json.encode()
    ).hexdigest()
    observation_payload = json.loads(candidate["enrichment_observation_payload_json"])
    observation_payload.update(
        {
            "observed_at": "2026-08-26T13:28:00+00:00",
            "bar_completed_at": "2026-08-26T13:29:00+00:00",
            "age_seconds": 60,
            "premarket_raw_payload_json": premarket_raw_json,
            "premarket_source_hash_sha256": candidate[
                "premarket_source_hash_sha256"
            ],
        }
    )
    observation_json = json.dumps(
        observation_payload, sort_keys=True, separators=(",", ":")
    )
    candidate.update(
        {
            "enrichment_observed_at": observation_payload["observed_at"],
            "enrichment_bar_completed_at": observation_payload[
                "bar_completed_at"
            ],
            "enrichment_observation_payload_json": observation_json,
            "enrichment_observation_sha256": hashlib.sha256(
                observation_json.encode()
            ).hexdigest(),
        }
    )
    enrichment_requested_at: list[str] = []

    class Candidate:
        def __init__(self, row):
            self._row = dict(row)
            self.ticker = str(self._row.get("ticker") or "")

        def to_dict(self):
            return dict(self._row)

    class FakeScanResult:
        run_id = "scan-alpha-cycle-e2e"
        created_at = "2026-08-26T13:30:00+00:00"
        config = {}

        def __init__(self, row):
            value = Candidate(row)
            self.all_candidates = [value]
            self.ranked_candidates = [value]
            self.top_explosive = [value]
            self.avoid_list = []

    class FakeScanService:
        def __init__(self, _provider, store=None):
            self.store = store

        def run(self, _config, *, persist=False, as_of=None):
            assert persist is True
            assert as_of == cycle_at
            return FakeScanResult(candidate)

    class FakeModel:
        def score_candidates(self, rows, _features, **_kwargs):
            assert rows
            return [dict(candidate)]

    original_record_historical = alpha_cycle_module.record_alpha_historical_signals
    original_persist_selection = alpha_cycle_module._persist_official_selections

    monkeypatch.setattr(
        alpha_cycle_module,
        "load_web_sources_config",
        lambda _path: SimpleNamespace(
            sources=[SimpleNamespace(enabled=True, fixture_path="fixture")]
        ),
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "web_auto_collect",
        lambda **_kwargs: {
            "status": "success",
            "rows": [dict(candidate)],
            "snapshot_path": str(tmp_path / "raw.csv"),
            "source_summary": {
                "status": "success",
                "candidate_count": 1,
                "eligible_count": 1,
            },
        },
    )

    def fake_premarket(rows, *, requested_at, **_kwargs):
        enrichment_requested_at.append(requested_at.isoformat())
        return {
            "ranking_rows": [dict(row) for row in rows],
            "summary": {"status": "complete"},
            "paths": {"snapshot": str(tmp_path / "enriched.csv")},
        }

    monkeypatch.setattr(alpha_cycle_module, "enrich_premarket_rows", fake_premarket)
    monkeypatch.setattr(
        alpha_cycle_module,
        "enrich_candidate_news",
        lambda rows, **_kwargs: {
            "rows": [dict(row) for row in rows],
            "summary": {"status": "complete"},
            "snapshot_path": str(tmp_path / "news.csv"),
        },
    )
    monkeypatch.setattr(alpha_cycle_module, "ScanService", FakeScanService)
    monkeypatch.setattr(alpha_cycle_module, "AlphaModel", FakeModel)
    monkeypatch.setattr(alpha_cycle_module, "write_scan_outputs", lambda *_args: {})
    monkeypatch.setattr(alpha_cycle_module, "build_source_reliability", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        alpha_cycle_module,
        "_verify_ranked_sec_safety",
        lambda rows, **_kwargs: (rows, {"status": "complete"}),
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "_apply_strategy_decision_receipts",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(alpha_cycle_module, "apply_alert_gates", lambda rows: rows)
    monkeypatch.setattr(
        alpha_cycle_module,
        "_persisted_strategy_receipt_verifier",
        lambda *args, **kwargs: lambda _row: True,
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "review_alpha_signals",
        lambda *_args, **_kwargs: {
            "decision": {
                "no_trade": True,
                "decision_tier": "no_trade",
                "reason": "test cycle evidence",
                "next_action": "retain research only",
            },
            "blocked": [],
        },
    )
    monkeypatch.setattr(alpha_cycle_module, "detect_regime", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        alpha_cycle_module,
        "_register_alpaca_screening_universe",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "active_alpha_v6_membership_by_ticker",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(alpha_cycle_module, "build_candidate_decisions", lambda **_kwargs: [])
    monkeypatch.setattr(
        alpha_cycle_module, "official_publication_rows", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "record_alpha_historical_signals",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "record_no_trade_historical_signal",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "format_alpha_no_trade",
        lambda **_kwargs: "test no trade",
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "_persist_official_selections",
        lambda *args, **kwargs: ([], {}),
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "_persist_research_radar_selections",
        lambda *args, **kwargs: ([], {}),
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "_persist_run_contract",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: {"status": "ok"}),
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "_dispatch",
        lambda *args, **kwargs: {"sent": 0, "skipped": 0, "errors": []},
    )
    monkeypatch.setattr(
        alpha_cycle_module,
        "_persist_notification_delivery_memberships",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(alpha_cycle_module, "_link_notification_events", lambda *args, **kwargs: {})

    db_path = tmp_path / "alpha-cycle.sqlite"
    result = alpha_cycle_module.alpha_cycle(
        config_path="fixture.yaml",
        db_path=db_path,
        out_dir=tmp_path / "cycle",
        notify="console",
        dry_run=True,
        as_of=cycle_at,
    )

    assert enrichment_requested_at == [cycle_timestamp]
    assert result["top_signal"]["timestamp"] == cycle_timestamp
    persisted = SQLiteScanStore(db_path).load_alpha_signals(scan_id="scan-alpha-cycle-e2e")
    assert len(persisted) == 1
    assert persisted[0]["timestamp"] == cycle_timestamp
    assert persisted[0]["alphaops_market_structure_plan"]["status"] == "COMPLETE"

    # Continue from the persisted cycle output through the real historical
    # ledger, official frozen cohort, price-observation store, and watcher.
    store = SQLiteScanStore(db_path)
    original_record_historical(
        store,
        persisted,
        source_summary=result["source_summary"],
    )
    frozen_slate = result["ranked_research_slate"]
    frozen_signal = dict(frozen_slate["rows"][0])
    event = alpha_cycle_module._official_selection_notification_event(
        "scan-alpha-cycle-e2e",
        "alpha_morning_watch",
        "Dawnstrike Alpha Watch",
        "test frozen selection",
        selected_signals=[frozen_signal],
        research_signals=[],
    )
    selected_rows, _ = original_persist_selection(
        store,
        scan_id="scan-alpha-cycle-e2e",
        selected_signals=[frozen_signal],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=cycle_timestamp,
        event=event,
        slate=frozen_slate,
    )
    assert len(selected_rows) == 1

    _, quote_observation = _watcher_signal()
    quote_observation = dict(quote_observation)
    quote_observation.update(
        {
            "observation_id": "obs-alpha-cycle-e2e",
            "signal_id": persisted[0]["signal_key"],
            "market_date": "2026-08-26",
            "ticker": "NOVA",
            "requested_at": cycle_timestamp,
            "price_type": "quote_ask",
            "source_kind": "alpaca_market_data",
            "provider": "alpaca",
            "provider_status": "verified",
            "tolerance_seconds": 360,
            "is_usable": True,
            "created_at": cycle_timestamp,
            "quote_freshness_seconds": 0.123456,
        }
    )
    quote_observation["payload_json"] = dict(quote_observation)
    store.persist_price_observations([quote_observation])
    loaded_quote = store.load_price_observations(
        market_date="2026-08-26", ticker="NOVA", usable_only=True
    )[0]
    assert loaded_quote["quote_ask"] == 10.0
    watch_signal = trade_watcher_module._watch_signals(store, market_date="2026-08-26")[0]
    assert luna_slate_module._source_bound_plan_observations(
        watch_signal,
        watch_signal["alphaops_market_structure_plan"],
        expected_ticker="NOVA",
    ), json.dumps(watch_signal, indent=2, sort_keys=True)
    lineage = trade_watcher_module._selection_lineage(watch_signal)
    assert lineage.get("selection_ids"), json.dumps(lineage, indent=2, sort_keys=True)
    strict_lineage = luna_slate_module._frozen_lineage_for_validation(watch_signal, "NOVA")
    assert "_invalid" not in strict_lineage, json.dumps(
        {
            "strict_lineage": strict_lineage,
            "scan_id": watch_signal.get("scan_id"),
            "frozen_slate_lineage": watch_signal.get("frozen_slate_lineage"),
            "selection_payload_json": watch_signal.get("selection_payload_json"),
        },
        indent=2,
        sort_keys=True,
    )
    direct_trace = evaluate_v5_official_paper(
        watch_signal,
        loaded_quote,
        simulated_equity=100_000,
        existing_symbol_notional=0.0,
        decision_time=cycle_timestamp,
    ).to_dict()
    direct_proof = _build_watcher_current_proof(watch_signal, loaded_quote, direct_trace)
    assert direct_proof is not None, json.dumps(direct_trace, indent=2, sort_keys=True)
    monkeypatch.setattr(
        trade_watcher_module,
        "collect_price_observations",
        lambda **_kwargs: {
            "status": "success",
            "source": "alpaca",
            "requested_at": cycle_timestamp,
            "market_date": "2026-08-26",
            "target_count": 1,
            "usable_count": 1,
            "rejected_count": 0,
            "observations": [loaded_quote],
        },
    )
    monkeypatch.setattr(
        trade_watcher_module,
        "_dispatch_notifications",
        lambda *args, **kwargs: {"sent": 0, "skipped": 0},
    )
    watcher = run_trade_watcher(
        db_path=db_path,
        mode="paper_execute",
        source="alpaca",
        market_date="2026-08-26",
        requested_at=cycle_timestamp,
        notify="console",
        dry_run=True,
        config=load_config(database_path=db_path),
    )
    assert watcher["intents"], json.dumps(watcher, indent=2, sort_keys=True)
    assert watcher["intents"][0]["action"] == "ENTER_LONG"
    assert watcher["monitor_publication_receipt"]["publication_tier"] == ("ALERTABLE_PAPER_ENTRY")
    assert len(store.load_monitor_publication_receipts(market_date="2026-08-26")) == 1


def test_gross_only_ratio_cannot_qualify_tier_two() -> None:
    assert _valid_modeled_cost_receipt({"reward_risk_ratio": 1.5}, "a" * 64) is False


def test_alpaca_daily_bar_completion_is_after_interval_start(monkeypatch) -> None:
    provider = AlpacaProvider.__new__(AlpacaProvider)
    provider.feed = "iex"
    provider.base_url = "https://data.alpaca.markets"
    monkeypatch.setattr(
        provider,
        "_request_json",
        lambda *_args, **_kwargs: {"bars": {"NOVA": [{"t": "2026-08-25T00:00:00Z", "h": 12.0}]}},
    )
    result = provider.get_previous_daily_highs(
        ["NOVA"],
        market_date="2026-08-26",
        config=type("C", (), {"historical_intraday_page_limit": 100})(),
        available_at=datetime.fromisoformat("2026-08-26T13:00:00+00:00"),
    )
    assert result["NOVA"]["completed_at"] == "2026-08-26T13:00:00+00:00"
    assert result["NOVA"]["completion_semantics"] == "availability_boundary"
    assert len(result["NOVA"]["raw_payload_json"]) > 0


def test_modeled_cost_receipt_recomputes_frozen_plan_math() -> None:
    row = _production_row()
    payload = _signal_payload(
        _attach_authenticated_alpaca_structure(row, decision_at="2026-08-26T13:30:00+00:00"),
        "scan",
        "2026-08-26T13:30:00+00:00",
        1,
    )
    receipt = _build_modeled_cost_receipt(payload)
    assert receipt is not None
    payload["modeled_cost_receipt"] = receipt
    assert _valid_modeled_cost_receipt(payload, payload["plan_hash_sha256"])
    receipt["reward_per_share_after_cost"] += 1.0
    assert not _valid_modeled_cost_receipt(payload, payload["plan_hash_sha256"])


def test_short_modeled_cost_receipt_uses_adverse_side_math() -> None:
    row = {
        "alphaops_market_structure_plan": {
            "status": "COMPLETE",
            "direction": "short",
            "entry": 10.0,
            "stop": 11.0,
            "target": 7.0,
            "plan_hash_sha256": "f" * 64,
        }
    }
    receipt = _build_modeled_cost_receipt(row)
    assert receipt is not None
    assert receipt["direction"] == "short"
    row["modeled_cost_receipt"] = receipt
    assert _valid_modeled_cost_receipt(row, "f" * 64)


def test_monitor_receipt_reuse_is_idempotent_and_divergence_fails(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "monitor.sqlite")
    receipt = {
        "receipt_id": "monitor-1",
        "market_date": "2026-08-26",
        "ticker": "NOVA",
        "signal_id": "sig-1",
        "plan_hash_sha256": "a" * 64,
        "content_hash_sha256": "b" * 64,
        "publication_count": 1,
        "checked_at": "2026-08-26T13:30:00+00:00",
    }
    assert store.persist_monitor_publication_receipts([receipt])["inserted"] == 1
    assert store.persist_monitor_publication_receipts([receipt])["reused"] == 1
    with pytest.raises(Exception, match="collision"):
        store.persist_monitor_publication_receipts([{**receipt, "ticker": "BAD"}])


def test_publication_without_modeled_cost_receipt_stays_tier_one() -> None:
    row = {"ticker": "NOVA", "publication_tier": TIER1}
    assert apply_publication_semantics([row], slate={"rows": [row]})[0]["publication_tier"] == TIER1


def _watcher_signal() -> tuple[dict[str, object], dict[str, object]]:
    row = _production_row()
    row["signal_id"] = "sig-nova"
    payload = _signal_payload(
        _attach_authenticated_alpaca_structure(row, decision_at="2026-08-26T13:30:00+00:00"),
        "scan-watcher",
        "2026-08-26T13:30:00+00:00",
        1,
    )
    payload.update(
        {
            "market_date": "2026-08-26",
            "signal_id": "sig-nova",
            "scan_id": "scan-watcher",
            "decision": "clean_edge",
            "decision_tier": "clean_edge",
            "alert_gate_status": "PASS",
            "manual_confirmation_required": False,
            "source_confidence": 92,
            "source_count": 3,
            "source_quality_status": "verified",
            "freshness_status": "FRESH",
            "input_status": "VERIFIED",
            "evidence_status": "VERIFIED",
            "stale_data_flag": False,
            "float_shares": 8_000_000,
            "float_status": "verified",
            "float_source": "verified_snapshot",
            "catalyst_summary": "FDA clearance announced before market open",
            "catalyst_url": "https://example.test/catalyst",
            "catalyst_status": "verified",
            "catalyst_tier": "A",
            "halt_status": "clear",
            "sec_risk_status": "clear",
            "corporate_action_status": "clear",
            "dollar_volume": 5_000_000,
            "previous_close": 8.0,
            "premarket_price": 10.0,
            "premarket_volume": 500_000,
            "gap_pct": 25.0,
            "spread_pct": 0.5,
            "liquidity_tier": "high_liquidity",
        }
    )
    # The legacy fixture's source bar is intentionally older than the quote
    # proof clock. Rebind only the immutable slate input to a short, valid
    # observation window so this operational fixture can satisfy production
    # slate validation without changing the watcher quote scenario.
    slate_input = dict(payload)
    observation_payload = json.loads(slate_input["enrichment_observation_payload_json"])
    premarket_raw = json.loads(slate_input["premarket_raw_payload_json"])
    premarket_raw["requested_at"] = "2026-08-26T13:02:00+00:00"
    premarket_raw_json = json.dumps(premarket_raw, sort_keys=True, separators=(",", ":"))
    observation_payload["premarket_raw_payload_json"] = premarket_raw_json
    observation_payload["premarket_source_hash_sha256"] = hashlib.sha256(
        premarket_raw_json.encode()
    ).hexdigest()
    observation_json = json.dumps(observation_payload, sort_keys=True, separators=(",", ":"))
    slate_input["enrichment_observation_payload_json"] = observation_json
    slate_input["enrichment_observation_sha256"] = hashlib.sha256(
        observation_json.encode()
    ).hexdigest()
    slate = build_ranked_research_slate(
        [slate_input],
        target=1,
        market_date="2026-08-26",
        generated_at="2026-08-26T13:02:00+00:00",
        scan_id="scan-watcher",
        require_safety=True,
    )
    payload.update(
        {
            "selection_id": "selection-nova",
            "cohort": "official_telegram",
            "frozen_ranked_research_slate": slate,
            "frozen_slate_lineage": {
                "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
                "slate_id": slate["slate_id"],
                "slate_content_hash_sha256": slate["content_hash_sha256"],
                "frozen_source_scan_id": "scan-watcher",
                "current_scan_id": "scan-watcher",
                "reuse_status": "CURRENT_SCAN",
            },
        }
    )
    quote_raw = {
        "ticker": "NOVA",
        "quote": {
            "t": "2026-08-26T13:30:00+00:00",
            "bp": 9.9,
            "ap": 10.0,
        },
    }
    quote_raw_json = json.dumps(quote_raw, sort_keys=True, separators=(",", ":"))
    return payload, {
        "quote_bid": 9.9,
        "quote_ask": 10.0,
        "price": 10.0,
        "observed_at": quote_raw["quote"]["t"],
        "quote_observed_at": quote_raw["quote"]["t"],
        "requested_at": "2026-08-26T13:30:00+00:00",
        "freshness_seconds": 0,
        "source": "alpaca_market_data_iex",
        "quote_source": "alpaca_market_data_iex",
        "source_bar_hash_sha256": "c" * 64,
        "quote_source_hash_sha256": hashlib.sha256(quote_raw_json.encode()).hexdigest(),
        "quote_raw_payload_json": quote_raw_json,
        "is_usable": True,
    }


def _watcher_trace(signal: dict[str, object], observation: dict[str, object]) -> dict[str, object]:
    decision = evaluate_v5_official_paper(
        signal,
        observation,
        simulated_equity=100_000,
        existing_symbol_notional=0.0,
        decision_time=str(observation["requested_at"]),
    )
    assert decision.eligible_for_official_paper, decision.reasons
    return decision.to_dict()


def test_watcher_proof_requires_valid_frozen_lineage_and_strict_identity() -> None:
    signal, observation = _watcher_signal()
    trace = _watcher_trace(signal, observation)
    proof = _build_watcher_current_proof(signal, observation, trace)
    assert proof is None or validate_watcher_current_proof(
        {**signal, "current_price": observation["quote_ask"], "watcher_current_proof": proof}
    )
    assert proof is not None
    tampered = {**proof, "ticker": "BAD"}
    assert not validate_watcher_current_proof(
        {**signal, "current_price": observation["quote_ask"], "watcher_current_proof": tampered}
    )
    no_lineage = {
        key: value for key, value in signal.items() if key != "frozen_ranked_research_slate"
    }
    assert _build_watcher_current_proof(no_lineage, observation, trace) is None
    bad_slate = dict(signal["frozen_ranked_research_slate"])
    bad_slate["content_hash_sha256"] = "d" * 64
    bad_signal = {**signal, "frozen_ranked_research_slate": bad_slate}
    assert _build_watcher_current_proof(bad_signal, observation, trace) is None


@pytest.mark.parametrize("invalid", (0.0, -1.0, float("nan"), float("inf")))
def test_watcher_rejects_invalid_primary_quote_and_row_aliases(invalid: float) -> None:
    signal, observation = _watcher_signal()
    trace = _watcher_trace(signal, observation)
    proof = _build_watcher_current_proof(signal, observation, trace)
    assert proof is not None

    invalid_quote_receipt = {
        **proof["quote_receipt"],
        "last": invalid,
        "price": 10.0,
    }
    invalid_quote = {
        **proof,
        "quote_receipt": invalid_quote_receipt,
        "quote_hash_sha256": _hash(invalid_quote_receipt),
    }
    invalid_quote["proof_hash_sha256"] = _hash(
        {key: value for key, value in invalid_quote.items() if key != "proof_hash_sha256"}
    )
    invalid_row = {
        **signal,
        "current_price": invalid,
        "current_quote_price": 10.0,
        "watcher_current_proof": proof,
    }
    assert not validate_watcher_current_proof(invalid_row)
    assert not validate_watcher_current_proof(
        {**signal, "current_price": 10.0, "watcher_current_proof": invalid_quote}
    )


@pytest.mark.parametrize("missing", (None, "", "   "))
def test_watcher_allows_missing_primary_quote_and_row_alias_fallback(missing: object) -> None:
    signal, observation = _watcher_signal()
    trace = _watcher_trace(signal, observation)
    proof = _build_watcher_current_proof(signal, observation, trace)
    assert proof is not None
    quote = {**proof["quote_receipt"], "last": missing, "price": 10.0}
    fallback_proof = {
        **proof,
        "quote_receipt": quote,
        "quote_hash_sha256": _hash(quote),
    }
    fallback_proof["proof_hash_sha256"] = _hash(
        {key: value for key, value in fallback_proof.items() if key != "proof_hash_sha256"}
    )
    row = {
        **signal,
        "current_price": missing,
        "current_quote_price": 10.0,
        "watcher_current_proof": fallback_proof,
    }
    assert validate_watcher_current_proof(row)


@pytest.mark.parametrize("slate_kind", ["v2_unsafe", "v1"])
def test_frozen_lineage_validation_rejects_nonproduction_slates(
    slate_kind: str,
) -> None:
    signal, _ = _watcher_signal()
    slate = build_ranked_research_slate(
        [signal],
        target=1,
        generated_at="2026-08-26T13:30:00+00:00",
        market_date="2026-08-26",
        scan_id="scan-watcher",
        require_safety=False,
    )
    if slate_kind == "v1":
        slate = dict(slate)
        slate["schema_version"] = "dawnstrike.luna.ranked_research_slate.v1"
        slate.pop("require_safety", None)
        slate["content_hash_sha256"] = luna_slate_module._slate_content_hash(slate)
        slate["slate_id"] = "luna-slate-" + slate["content_hash_sha256"][:24]
    hostile = {
        **signal,
        "frozen_ranked_research_slate": slate,
        "frozen_slate_lineage": {
            **signal["frozen_slate_lineage"],
            "slate_id": slate["slate_id"],
            "slate_content_hash_sha256": slate["content_hash_sha256"],
        },
    }

    result = luna_slate_module._frozen_lineage_for_validation(hostile, "NOVA")

    assert result["_invalid"] == "frozen ranked slate failed validation"


def test_watcher_rejects_wrong_account_quote_ticker_plan_and_entry_window() -> None:
    signal, observation = _watcher_signal()
    trace = _watcher_trace(signal, observation)
    proof = _build_watcher_current_proof(signal, observation, trace)
    assert proof is not None
    for mutation in (
        {"simulated_account_id": "wrong-account"},
        {"ticker": "BAD"},
        {"plan_hash_sha256": "e" * 64},
        {"entry_window_status": "CLOSED"},
    ):
        mutated = json.loads(json.dumps(proof))
        if "simulated_account_id" in mutation:
            mutated["portfolio_receipt"].update(mutation)
        elif "entry_window_status" in mutation:
            mutated["quote_receipt"].update(mutation)
        else:
            mutated.update(mutation)
            for key in ("quote_receipt", "portfolio_receipt"):
                mutated[key].update(mutation)
            if "plan_hash_sha256" in mutation:
                mutated["evaluate_v5_official_paper"]["plan_hash_sha256"] = mutation[
                    "plan_hash_sha256"
                ]
                mutated["evaluate_v5_official_paper_trace"]["plan_hash_sha256"] = mutation[
                    "plan_hash_sha256"
                ]
        for key, hash_key in (
            ("quote_receipt", "quote_hash_sha256"),
            ("portfolio_receipt", "portfolio_hash_sha256"),
        ):
            mutated[hash_key] = hashlib.sha256(
                json.dumps(mutated[key], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        mutated["proof_hash_sha256"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in mutated.items() if key != "proof_hash_sha256"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert not validate_watcher_current_proof(
            {**signal, "current_price": observation["quote_ask"], "watcher_current_proof": mutated}
        )


def test_monitor_receipt_binds_frozen_lineage_and_is_research_only() -> None:
    signal, observation = _watcher_signal()
    trace = _watcher_trace(signal, observation)
    proof = _build_watcher_current_proof(signal, observation, trace)
    assert proof is not None
    receipt = _monitor_publication_receipt(
        signal=signal,
        proof=proof,
        intent_id="intent-test",
        checked_at=proof["checked_at"],
    )
    assert receipt["selection_id"] == signal["selection_id"]
    assert receipt["source_scan_id"] == "scan-watcher"
    assert receipt["frozen_slate_id"] == signal["frozen_ranked_research_slate"]["slate_id"]
    assert receipt["publication_tier"] == "ALERTABLE_PAPER_ENTRY"
    assert receipt["broker_execution"] == "disabled"


def test_alpaca_quote_contract_survives_collect_persist_load(monkeypatch, tmp_path) -> None:
    class FakeAlpaca:
        def __init__(self, _config):
            pass

        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return [
                {
                    "ticker": symbols[0],
                    "timestamp": "2026-08-26T12:59:00Z",
                    "high": 10.0,
                    "low": 9.0,
                    "close": 10.0,
                    "volume": 100,
                }
            ]

        def get_latest_quotes(self, symbols, config):
            raw = {"t": "2026-08-26T13:00:00Z", "bp": 10.0, "ap": 10.1}
            raw_json = json.dumps(
                {"ticker": symbols[0], "quote": raw},
                sort_keys=True,
                separators=(",", ":"),
            )
            return {
                symbols[0]: {
                    "ticker": symbols[0],
                    "timestamp": raw["t"],
                    "bid": 10.0,
                    "ask": 10.1,
                    "source": "alpaca_market_data_iex",
                    "raw_payload_json": raw_json,
                    "source_hash_sha256": hashlib.sha256(raw_json.encode()).hexdigest(),
                }
            }

    monkeypatch.setattr(price_observation_module, "AlpacaProvider", FakeAlpaca)
    db_path = tmp_path / "quotes.sqlite"
    result = price_observation_module.collect_price_observations(
        db_path=db_path,
        source="alpaca",
        tickers=["NOVA"],
        market_date="2026-08-26",
        requested_at="2026-08-26T13:00:00+00:00",
        persist=True,
    )
    assert result["usable_count"] == 1
    loaded = SQLiteScanStore(db_path).load_price_observations(
        market_date="2026-08-26", ticker="NOVA", usable_only=True
    )[0]
    assert loaded["quote_bid"] == 10.0
    assert loaded["quote_ask"] == 10.1
    assert loaded["quote_freshness_seconds"] == 0.0
    assert loaded["quote_source"].startswith("alpaca_market_data_")
    record = SQLiteScanStore(db_path).load_price_observation_records(
        market_date="2026-08-26", ticker="NOVA"
    )[0]
    assert record["payload_json"]["quote_bid"] == 10.0
    assert record["payload_json"]["quote_freshness_seconds"] == 0.0
    monkeypatch.setattr(
        FakeAlpaca,
        "get_latest_quotes",
        lambda _self, _symbols, _config: (_ for _ in ()).throw(
            DataProviderError("quote unavailable")
        ),
    )
    degraded = price_observation_module.collect_price_observations(
        db_path=db_path,
        source="alpaca",
        tickers=["NOVA"],
        market_date="2026-08-26",
        requested_at="2026-08-26T13:00:00+00:00",
        persist=True,
    )
    assert degraded["usable_count"] == 1
    assert (
        "quote_bid"
        not in SQLiteScanStore(db_path).load_price_observations(
            market_date="2026-08-26", ticker="NOVA", usable_only=True
        )[0]
    )


def test_quote_credential_validation_failure_preserves_bar_monitoring(
    monkeypatch, tmp_path
) -> None:
    class FailingQuoteCredentialsAlpaca:
        validation_calls = 0

        def __init__(self, _config):
            pass

        def validate_credentials(self):
            type(self).validation_calls += 1
            if type(self).validation_calls == 2:
                raise DataProviderError("quote credentials unavailable")

        def get_minute_bars(self, symbols, start, end, config):
            return [
                {
                    "ticker": symbols[0],
                    "timestamp": "2026-08-26T12:59:00Z",
                    "high": 10.0,
                    "low": 9.0,
                    "close": 10.0,
                    "volume": 100,
                }
            ]

    monkeypatch.setattr(price_observation_module, "AlpacaProvider", FailingQuoteCredentialsAlpaca)
    db_path = tmp_path / "credential-outage.sqlite"
    result = price_observation_module.collect_price_observations(
        db_path=db_path,
        source="alpaca",
        tickers=["NOVA"],
        market_date="2026-08-26",
        requested_at="2026-08-26T13:00:00+00:00",
        persist=True,
    )
    assert result["usable_count"] == 1
    observation = SQLiteScanStore(db_path).load_price_observations(
        market_date="2026-08-26", ticker="NOVA", usable_only=True
    )[0]
    assert observation["price"] == 10.0
    assert "quote_bid" not in observation

    signal, _ = _watcher_signal()
    signal["market_date"] = "2026-08-26"
    exit_decision = _decision_for_signal(
        signal=signal,
        observation=observation,
        open_position={
            "position_id": "position-1",
            "signal_id": signal["signal_id"],
            "ticker": "NOVA",
            "status": "OPEN",
            "entry_price": 9.5,
            "stop_price": 9.0,
            "target_price": 12.0,
            "quantity": 10,
            "notional": 95.0,
        },
        prior_entry=True,
        settings=WatcherSettings(),
        open_count=1,
        daily_entry_count=1,
        existing_symbol_notional=95.0,
        scanner_config=load_config(),
    )
    assert exit_decision["state"] == "PAPER_OPEN"


def test_alpaca_quote_collect_to_watcher_creates_one_paper_receipt(monkeypatch, tmp_path) -> None:
    signal, _ = _watcher_signal()
    signal["scan_id"] = "scan-watcher"
    signal["market_date"] = "2026-08-26"
    signal["selected_at"] = "2026-08-26T13:20:00+00:00"
    signal.update(
        {
            "alert_gate_status": "PASS",
            "manual_confirmation_required": False,
            "source_confidence": 92,
            "source_count": 3,
            "source_quality_status": "verified",
            "stale_data_flag": False,
            "float_shares": 8_000_000,
            "float_status": "verified",
            "float_source": "verified_snapshot",
            "catalyst_summary": "FDA clearance announced before market open",
            "catalyst_url": "https://example.test/catalyst",
            "catalyst_status": "verified",
            "catalyst_tier": "A",
            "halt_status": "clear",
            "sec_risk_status": "clear",
            "corporate_action_status": "clear",
            "dollar_volume": 5_000_000,
            "previous_close": 8.0,
            "premarket_price": 10.0,
            "premarket_volume": 500_000,
            "gap_pct": 25.0,
            "spread_pct": 0.5,
            "liquidity_tier": "high_liquidity",
        }
    )
    slate = signal["frozen_ranked_research_slate"]
    lineage = signal["frozen_slate_lineage"]
    store = SQLiteScanStore(tmp_path / "operational.sqlite")
    store.persist_historical_signals([{**signal, "raw_payload_json": signal}])
    store.persist_signal_selections(
        [
            {
                "selection_id": signal["selection_id"],
                "scan_id": "scan-watcher",
                "signal_id": signal["signal_id"],
                "ticker": "NOVA",
                "rank": 1,
                "strategy_id": "alphaops_v5",
                "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
                "cohort": "official_telegram",
                "decision": "clean_edge",
                "selected_at": "2026-08-26T13:20:00+00:00",
                "event_key": "alphaops:scan-watcher:alpha_morning_watch",
                "body_sha256": "watcher-body",
                "payload_json": {
                    "signal": signal,
                    "frozen_ranked_research_slate": slate,
                    "frozen_slate_lineage": lineage,
                },
            }
        ]
    )
    persisted_signal = store.load_historical_signals(market_date="2026-08-26")[0]
    assert persisted_signal["raw_payload_json"].get("previous_close") == 8.0, persisted_signal[
        "raw_payload_json"
    ]

    class FakeAlpaca:
        def __init__(self, _config):
            pass

        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return [
                {
                    "ticker": symbols[0],
                    "timestamp": "2026-08-26T13:29:00Z",
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 100,
                }
            ]

        def get_latest_quotes(self, symbols, config):
            raw = {
                "t": "2026-08-26T13:30:00Z",
                "bp": 9.9,
                "ap": 10.0,
            }
            raw_json = json.dumps(
                {"ticker": symbols[0], "quote": raw},
                sort_keys=True,
                separators=(",", ":"),
            )
            return {
                symbols[0]: {
                    "ticker": symbols[0],
                    "timestamp": raw["t"],
                    "bid": 9.9,
                    "ask": 10.0,
                    "source": "alpaca_market_data_iex",
                    "raw_payload_json": raw_json,
                    "source_hash_sha256": hashlib.sha256(raw_json.encode()).hexdigest(),
                }
            }

    monkeypatch.setattr(price_observation_module, "AlpacaProvider", FakeAlpaca)
    monkeypatch.setattr(
        trade_watcher_module,
        "_utc_now",
        lambda: "2026-08-26T13:30:00+00:00",
    )
    first = run_trade_watcher(
        db_path=tmp_path / "operational.sqlite",
        source="alpaca",
        market_date="2026-08-26",
        requested_at="2026-08-26T13:30:00+00:00",
        dry_run=True,
        notify="console",
    )
    assert first["intent_stats"]["inserted"] == 1
    assert first["monitor_publication_stats"]["inserted"] == 1
    assert first["intents"][0]["action"] == "ENTER_LONG"
    assert first["intents"][0]["decision_price"] == 10.0
    receipts = store.load_monitor_publication_receipts(market_date="2026-08-26")
    assert len(receipts) == 1
    assert receipts[0]["publication_tier"] == "ALERTABLE_PAPER_ENTRY"

    second = run_trade_watcher(
        db_path=tmp_path / "operational.sqlite",
        source="alpaca",
        market_date="2026-08-26",
        requested_at="2026-08-26T13:30:00+00:00",
        dry_run=True,
        notify="console",
    )
    assert second["intent_stats"]["inserted"] == 0
    assert second["monitor_publication_receipts"] == []
    assert second["monitor_publication_stats"] == {"inserted": 0, "reused": 0, "count": 0}
    assert len(store.load_monitor_publication_receipts(market_date="2026-08-26")) == 1


def test_v5_missing_quote_stands_down_without_intent() -> None:
    signal, observation = _watcher_signal()
    for key in (
        "quote_bid",
        "quote_ask",
        "quote_observed_at",
        "quote_source",
        "quote_source_hash_sha256",
        "quote_raw_payload_json",
    ):
        observation.pop(key, None)
    result = _decision_for_signal(
        signal=signal,
        observation=observation,
        open_position=None,
        prior_entry=False,
        settings=WatcherSettings(),
        open_count=0,
        daily_entry_count=0,
        existing_symbol_notional=0.0,
        scanner_config=load_config(),
    )
    assert result["state"] == "STAND_DOWN"
    assert not result.get("intent")


@pytest.mark.parametrize(
    ("direction", "for_exit", "expected"),
    [
        ("long", False, 10.0),
        ("short", False, 9.9),
        ("long", True, 9.9),
        ("short", True, 10.0),
    ],
)
def test_v5_quote_side_is_lifecycle_aware(direction, for_exit, expected) -> None:
    signal, observation = _watcher_signal()
    signal["alphaops_market_structure_plan"] = {
        **signal["alphaops_market_structure_plan"],
        "direction": direction,
    }
    selected = _side_aware_quote_observation(signal, observation, for_exit=for_exit)
    assert selected["price"] == expected
    assert selected["price_type"] == (
        f"quote_{'ask' if (direction == 'long') != for_exit else 'bid'}_side"
    )


def _rehash_watcher_proof(proof: dict[str, object]) -> dict[str, object]:
    """Rehash nested receipts and the envelope for adversarial replay tests."""

    mutated = json.loads(json.dumps(proof))
    for receipt_key, hash_key in (
        ("quote_receipt", "quote_hash_sha256"),
        ("portfolio_receipt", "portfolio_hash_sha256"),
    ):
        receipt = mutated[receipt_key]
        mutated[hash_key] = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    mutated["proof_hash_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in mutated.items() if key != "proof_hash_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return mutated


@pytest.mark.parametrize(
    ("field", "mutation"),
    [
        ("schema_version", lambda trace: trace.update({"schema_version": "forged.v1"})),
        ("signal_id", lambda trace: trace.update({"signal_id": "forged-signal"})),
        ("ticker", lambda trace: trace.update({"ticker": "BAD"})),
        ("plan_hash_sha256", lambda trace: trace.update({"plan_hash_sha256": "e" * 64})),
        ("direction", lambda trace: trace["computed"].update({"direction": "short"})),
        ("account_id", lambda trace: trace.update({"account_id": "wrong-account"})),
        (
            "eligibility",
            lambda trace: trace.update({"eligible_for_official_paper": False}),
        ),
        (
            "checks",
            lambda trace: trace.update({"checks": [*trace["checks"], {"forged": True}]}),
        ),
        (
            "computed",
            lambda trace: trace["computed"].update({"actual_after_cost_reward_risk": 99.0}),
        ),
    ],
)
def test_rehashed_watcher_trace_mutation_fails_strict_validator(field, mutation) -> None:
    signal, observation = _watcher_signal()
    trace = _watcher_trace(signal, observation)
    proof = _build_watcher_current_proof(signal, observation, trace)
    assert proof is not None
    forged = json.loads(json.dumps(proof))
    forged_trace = json.loads(json.dumps(trace))
    mutation(forged_trace)
    forged["evaluate_v5_official_paper"] = forged_trace
    forged["evaluate_v5_official_paper_trace"] = json.loads(json.dumps(forged_trace))
    forged = _rehash_watcher_proof(forged)
    assert not validate_watcher_current_proof(
        {
            **signal,
            "current_price": observation["quote_ask"],
            "watcher_current_proof": forged,
        }
    ), field


def test_watcher_proof_and_monitor_receipt_replay_is_deterministic() -> None:
    signal, observation = _watcher_signal()
    trace = _watcher_trace(signal, observation)
    first = _build_watcher_current_proof(signal, observation, trace)
    second = _build_watcher_current_proof(signal, observation, trace)
    assert first is not None and second is not None
    assert first["proof_hash_sha256"] == second["proof_hash_sha256"]
    first_receipt = _monitor_publication_receipt(
        signal=signal,
        proof=first,
        intent_id="intent-test",
        checked_at=first["checked_at"],
    )
    second_receipt = _monitor_publication_receipt(
        signal=signal,
        proof=second,
        intent_id="intent-test",
        checked_at=second["checked_at"],
    )
    assert first_receipt["receipt_id"] == second_receipt["receipt_id"]
    assert first_receipt["content_hash_sha256"] == second_receipt["content_hash_sha256"]


def test_changed_quote_creates_new_governed_watcher_receipt() -> None:
    signal, observation = _watcher_signal()
    trace = _watcher_trace(signal, observation)
    first = _build_watcher_current_proof(signal, observation, trace)
    assert first is not None
    changed = json.loads(json.dumps(observation))
    changed_raw = {
        "ticker": "NOVA",
        "quote": {"t": "2026-08-26T13:31:00+00:00", "bp": 9.91, "ap": 10.01},
    }
    changed_raw_json = json.dumps(changed_raw, sort_keys=True, separators=(",", ":"))
    changed.update(
        {
            "price": 10.01,
            "quote_bid": 9.91,
            "quote_ask": 10.01,
            "quote_observed_at": "2026-08-26T13:31:00+00:00",
            "observed_at": "2026-08-26T13:31:00+00:00",
            "requested_at": "2026-08-26T13:31:00+00:00",
            "quote_source_hash_sha256": hashlib.sha256(changed_raw_json.encode()).hexdigest(),
            "quote_raw_payload_json": changed_raw_json,
        }
    )
    changed_trace = _watcher_trace(signal, changed)
    second = _build_watcher_current_proof(signal, changed, changed_trace)
    assert second is not None
    assert second["checked_at"] != first["checked_at"]
    first_receipt = _monitor_publication_receipt(
        signal=signal,
        proof=first,
        intent_id="intent-test",
        checked_at=first["checked_at"],
    )
    second_receipt = _monitor_publication_receipt(
        signal=signal,
        proof=second,
        intent_id="intent-test",
        checked_at=second["checked_at"],
    )
    assert second_receipt["receipt_id"] != first_receipt["receipt_id"]
    assert second_receipt["content_hash_sha256"] != first_receipt["content_hash_sha256"]


def _short_watcher_signal() -> tuple[dict[str, object], dict[str, object]]:
    signal, observation = _watcher_signal()
    source_plan = signal["alphaops_market_structure_plan"]
    assert isinstance(source_plan, dict)
    source_observations = {
        str(item["role"]): dict(item)
        for item in source_plan["observations"]
        if isinstance(item, dict)
    }
    for role, value in (("entry", 10.0), ("stop", 11.0), ("target", 7.0)):
        source_observations[role]["value"] = value
        source_observations[role]["raw_value"] = value
        source_observations[role].pop("observation_hash", None)
        source_observations[role]["observation_hash"] = hashlib.sha256(
            json.dumps(source_observations[role], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    short_plan = construct_alphaops_v5_plan(
        {
            "ticker": "NOVA",
            "direction": "short",
            "entry_watch_level": 10.0,
            "invalidation_level": 11.0,
            "target_1": 7.0,
            "target_basis_kind": "prior_day_resistance",
            "target_derived_from_risk": False,
            "market_structure_observations": source_observations,
        },
        decision_at="2026-08-26T13:30:00+00:00",
    )
    assert short_plan.status == "COMPLETE"
    signal.update(
        {
            "direction": "short",
            "entry_watch_level": 10.0,
            "invalidation_level": 11.0,
            "target_1": 7.0,
            "target_basis_kind": "prior_day_resistance",
            "target_derived_from_risk": False,
            "alphaops_market_structure_plan": short_plan.to_dict(),
            "plan_hash_sha256": short_plan.plan_hash_sha256,
        }
    )
    observation["price"] = 9.9
    return signal, observation


def test_short_policy_and_paper_lifecycle_is_direction_aware(tmp_path) -> None:
    signal, observation = _short_watcher_signal()
    trace = _watcher_trace(signal, observation)
    assert trace["computed"]["direction"] == "short"
    assert trace["computed"]["expected_entry_price"] < observation["price"]
    assert trace["computed"]["expected_stop_exit_price"] > signal["invalidation_level"]
    assert trace["computed"]["expected_target_exit_price"] > signal["target_1"]
    # Short lifecycle accounting remains testable in the research/paper-plan
    # path, while the official v5 admission path is borrow-truth gated below.
    lifecycle_signal = dict(signal)
    lifecycle_signal.update(
        {
            "strategy_id": "alphaops_v4",
            "strategy_version": "dawnstrike-alphaops-v4",
        }
    )
    lifecycle_signal.update(build_episode_identity(lifecycle_signal).to_dict())
    entry = _entry_decision(
        lifecycle_signal,
        observation,
        settings=WatcherSettings(),
        open_count=0,
        daily_entry_count=0,
        existing_symbol_notional=0.0,
    )
    assert entry["state"] == "ENTRY_TRIGGERED"
    intent = entry["intent"]
    assert intent["action"] == ACTION_ENTER_SHORT
    position, entry_fill = _open_paper_position(intent, load_config())
    assert position["direction"] == "short"
    assert entry_fill["side"] == "SELL_SHORT"

    target_observation = json.loads(json.dumps(observation))
    target_observation.update(
        {
            "price": 6.9,
            "quote_bid": 6.8,
            "quote_ask": 6.9,
            "quote_observed_at": "2026-08-26T13:31:00+00:00",
            "observed_at": "2026-08-26T13:31:00+00:00",
            "requested_at": "2026-08-26T13:31:00+00:00",
        }
    )
    exit_decision = _decision_for_signal(
        signal=lifecycle_signal,
        observation=target_observation,
        open_position=position,
        prior_entry=True,
        settings=WatcherSettings(),
        open_count=1,
        daily_entry_count=1,
        existing_symbol_notional=position["notional"],
        scanner_config=load_config(),
    )
    assert exit_decision["state"] == "EXIT_TRIGGERED"
    assert exit_decision["intent"]["action"] == ACTION_EXIT_SHORT
    closed, target_fill = _close_paper_position(position, exit_decision["intent"], load_config())
    assert target_fill["side"] == "BUY_TO_COVER"
    assert closed["realized_pnl"] > 0

    stop_observation = json.loads(json.dumps(observation))
    stop_observation.update(
        {
            "price": 11.2,
            "quote_bid": 11.1,
            "quote_ask": 11.2,
            "quote_observed_at": "2026-08-26T13:32:00+00:00",
            "observed_at": "2026-08-26T13:32:00+00:00",
            "requested_at": "2026-08-26T13:32:00+00:00",
        }
    )
    stop_decision = _decision_for_signal(
        signal=lifecycle_signal,
        observation=stop_observation,
        open_position=position,
        prior_entry=True,
        settings=WatcherSettings(),
        open_count=1,
        daily_entry_count=1,
        existing_symbol_notional=position["notional"],
        scanner_config=load_config(),
    )
    assert stop_decision["state"] == "EXIT_TRIGGERED"
    stopped, stop_fill = _close_paper_position(position, stop_decision["intent"], load_config())
    assert stop_fill["side"] == "BUY_TO_COVER"
    assert stopped["realized_pnl"] < 0

    store = SQLiteScanStore(tmp_path / "short-lifecycle.sqlite")
    store.persist_trade_watcher_lifecycle(
        intents=[intent, exit_decision["intent"]],
        paper_positions=[position, closed],
        paper_fills=[entry_fill, target_fill],
        signal_events=[],
    )
    loaded_position = store.load_paper_positions(signal_id=signal["signal_id"])[0]
    loaded_fills = store.load_paper_trade_fills(signal_id=signal["signal_id"])
    assert loaded_position["direction"] == "short"
    assert loaded_position["status"] == "CLOSED"
    assert {fill["side"] for fill in loaded_fills} == {"SELL_SHORT", "BUY_TO_COVER"}


@pytest.mark.parametrize(
    "borrow_receipt",
    [
        None,
        {
            "schema_version": "alpaca.asset.borrow.v1",
            "ticker": "NOVA",
            "shortable": True,
            "easy_to_borrow": True,
            "observed_at": "2026-08-25T13:30:00+00:00",
        },
        {
            "schema_version": "alpaca.asset.borrow.v1",
            "ticker": "WRONG",
            "shortable": True,
            "easy_to_borrow": True,
            "observed_at": "2026-08-26T13:30:00+00:00",
        },
    ],
)
def test_short_v5_admission_requires_authenticated_current_borrow_truth(
    borrow_receipt,
) -> None:
    signal, observation = _short_watcher_signal()
    if borrow_receipt is not None:
        observation["short_borrow_receipt"] = borrow_receipt
    decision = _entry_decision(
        signal,
        observation,
        settings=WatcherSettings(),
        open_count=0,
        daily_entry_count=0,
        existing_symbol_notional=0.0,
    )
    assert decision["state"] == "STAND_DOWN"
    assert "BORROW_TRUTH_UNAVAILABLE" in decision["reason"]
    assert "intent" not in decision
    trace = _watcher_trace(signal, observation)
    assert _build_watcher_current_proof(signal, observation, trace) is None
