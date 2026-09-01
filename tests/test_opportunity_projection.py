from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_opportunity_pipeline import (  # noqa: E402
    NOW,
    _execution_risk_for,
    _finalized_two_strategy_pipeline,
    _pipeline_risk_policy,
    _pipeline_universe,
    _two_candidate_dataset,
)

from intraday_scanner.dashboard.opportunity_projection import (  # noqa: E402
    NO_QUALIFYING_MESSAGE,
    OpportunityProjection,
    OpportunityProjectionReason,
    OpportunityProjectionState,
    OpportunityRowProjection,
    build_opportunity_projection,
    disabled_projection,
    unavailable_projection,
)
from intraday_scanner.dashboard.opportunity_projection_render import (  # noqa: E402
    render_streamlit_opportunity_projection,
)
from intraday_scanner.dashboard.opportunity_projection_store import (  # noqa: E402
    _market_date_for_timestamp,
    load_latest_opportunity_projection,
    opportunity_projection_enabled,
    write_public_opportunity_projection,
)
from intraday_scanner.storage.opportunity_store import OpportunityStore  # noqa: E402
from intraday_scanner.v2.opportunity.expectancy import (  # noqa: E402
    build_expectancy_evidence,
)
from intraday_scanner.v2.opportunity.features import FeatureConfig  # noqa: E402
from intraday_scanner.v2.opportunity.models import EvaluationStatus  # noqa: E402
from intraday_scanner.v2.opportunity.pipeline import (  # noqa: E402
    PipelineResult,
    build_strategy_expectancy_binding,
    prepare_opportunity_pipeline,
    run_opportunity_pipeline,
)
from intraday_scanner.v2.opportunity.registry import (  # noqa: E402
    StrategyRegistry,
    build_default_registry,
)

RECORDED_AT = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def no_qualifying_result() -> PipelineResult:
    return _finalized_two_strategy_pipeline()[2]


@pytest.fixture(scope="module")
def qualifying_result() -> PipelineResult:
    dataset = _two_candidate_dataset()
    default = build_default_registry()
    momentum = default.get("DS-MOM-001")
    disabled = default.get("DS-OF-001")
    expectancy = build_expectancy_evidence(
        (Decimal("1"),) * 120 + (Decimal("-1"),) * 80,
        cohort_id="wp007-projection-fixture",
        min_sample_size=100,
    )
    binding = build_strategy_expectancy_binding(
        decision_at=NOW,
        strategy_definition=momentum,
        evidence=expectancy,
        observed_at=NOW,
        source_identity="wp007-projection-fixture",
        method="bounded fixture cohort calculation",
    )
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=_pipeline_universe(
            dataset,
            requested_symbols=("ABC", "DEF"),
        ),
        registry=StrategyRegistry((momentum, disabled)),
        expectancy_bindings=(binding,),
        sector_by_symbol={"ABC": "technology", "DEF": "industrials"},
        correlation_cluster_by_symbol={"ABC": "cluster-a", "DEF": "cluster-b"},
        feature_config=FeatureConfig(min_cross_section_size=2),
    )
    risks = {
        item.evaluation_id: _execution_risk_for(item)
        for item in prepared.evaluations
        if item.status is EvaluationStatus.ELIGIBLE
    }
    return run_opportunity_pipeline(
        prepared,
        risk_by_evaluation=risks,
        risk_policy=_pipeline_risk_policy(),
    )


@pytest.mark.parametrize("value", ("1", "true", "TRUE", " yes ", "on", "ON"))
def test_feature_flag_accepts_only_normalized_explicit_true_values(value: str) -> None:
    assert opportunity_projection_enabled(value) is True


@pytest.mark.parametrize("value", (None, "", "0", "false", "y", "enabled", "2"))
def test_feature_flag_defaults_false(value: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAWNSTRIKE_OPPORTUNITY_PROJECTION_ENABLED", raising=False)
    assert opportunity_projection_enabled(value) is False


def test_disabled_path_performs_zero_database_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "must-not-open.sqlite"

    def fail_open(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise AssertionError("disabled projection opened the opportunity database")

    monkeypatch.setattr(
        "intraday_scanner.dashboard.opportunity_projection_store.connect_read_only",
        fail_open,
    )
    projection = load_latest_opportunity_projection(source, enabled=False)

    assert projection == disabled_projection()
    assert not source.exists()
    assert not list(tmp_path.glob("must-not-open.sqlite*"))


def test_missing_old_schema_and_no_run_are_data_unavailable(tmp_path: Path) -> None:
    missing = load_latest_opportunity_projection(tmp_path / "missing.sqlite", enabled=True)
    assert missing.state is OpportunityProjectionState.DATA_UNAVAILABLE
    assert missing.reason_code is OpportunityProjectionReason.DATABASE_MISSING

    old = tmp_path / "old.sqlite"
    with sqlite3.connect(old) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (26, ?)", (NOW.isoformat(),))
        connection.commit()
    old_hash = hashlib.sha256(old.read_bytes()).hexdigest()
    old_projection = load_latest_opportunity_projection(old, enabled=True)
    assert old_projection.reason_code is OpportunityProjectionReason.SCHEMA_UNSUPPORTED
    assert hashlib.sha256(old.read_bytes()).hexdigest() == old_hash

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database")
    corrupt_hash = hashlib.sha256(corrupt.read_bytes()).hexdigest()
    corrupt_projection = load_latest_opportunity_projection(corrupt, enabled=True)
    assert corrupt_projection.reason_code is OpportunityProjectionReason.DATABASE_INVALID
    assert hashlib.sha256(corrupt.read_bytes()).hexdigest() == corrupt_hash

    empty_database = tmp_path / "empty.sqlite"
    OpportunityStore(empty_database).initialize()
    no_run = load_latest_opportunity_projection(empty_database, enabled=True)
    assert no_run.reason_code is OpportunityProjectionReason.NO_PERSISTED_RUN


def test_latest_run_uses_read_only_store_replay_and_creates_no_sidecars(
    tmp_path: Path,
    no_qualifying_result: PipelineResult,
    qualifying_result: PipelineResult,
) -> None:
    database = tmp_path / "opportunity.sqlite"
    store = OpportunityStore(database)
    store.initialize()
    store.append_run(no_qualifying_result, recorded_at=RECORDED_AT)
    store.append_run(qualifying_result, recorded_at=RECORDED_AT + timedelta(seconds=1))
    before_sidecars = sorted(path.name for path in tmp_path.glob("opportunity.sqlite-*"))
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()

    projection = load_latest_opportunity_projection(database, enabled=True)

    assert projection.state is OpportunityProjectionState.QUALIFYING
    assert projection.source_run_id == qualifying_result.run_id
    assert [row.rank for row in projection.rows] == [1, 2]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert sorted(path.name for path in tmp_path.glob("opportunity.sqlite-*")) == before_sidecars


def test_latest_run_scope_rejects_historical_row_for_requested_market_date(
    tmp_path: Path,
    qualifying_result: PipelineResult,
) -> None:
    database = tmp_path / "historical.sqlite"
    store = OpportunityStore(database)
    store.initialize()
    store.append_run(qualifying_result, recorded_at=RECORDED_AT)

    current = load_latest_opportunity_projection(
        database,
        enabled=True,
        expected_market_date="2026-08-12",
    )
    matching = load_latest_opportunity_projection(
        database,
        enabled=True,
        expected_market_date=qualifying_result.decision_at.date().isoformat(),
    )

    assert current.state is OpportunityProjectionState.DATA_UNAVAILABLE
    assert current.reason_code is OpportunityProjectionReason.NO_PERSISTED_RUN
    assert matching.state is OpportunityProjectionState.QUALIFYING
    assert matching.source_run_id == qualifying_result.run_id


def test_market_date_uses_new_york_exchange_date_for_utc_cross_day_timestamp() -> None:
    assert _market_date_for_timestamp(
        datetime(2026, 8, 12, 0, 30, tzinfo=timezone.utc)
    ) == "2026-08-11"


def test_latest_run_scopes_by_exchange_date_and_orders_mixed_offsets_chronologically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _Connection:
        def execute(self, query: str):
            if query.startswith("PRAGMA query_only"):
                return SimpleNamespace(fetchone=lambda: (1,))
            if query.startswith("SELECT version"):
                return SimpleNamespace(fetchone=lambda: (30,))
            return SimpleNamespace(
                fetchall=lambda: [
                    (
                        "older",
                        "2026-08-12T00:30:00+00:00",
                        "2026-08-12T00:31:00+00:00",
                    ),
                    (
                        "newer",
                        "2026-08-11T23:30:00-04:00",
                        "2026-08-12T03:31:00+00:00",
                    ),
                ]
            )

        def close(self) -> None:
            return None

    class _Store:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def load_run(self, run_id: str) -> SimpleNamespace:
            assert run_id == "newer"
            return SimpleNamespace(
                decision_at=datetime(2026, 8, 11, 23, 30, tzinfo=timezone(timedelta(hours=-4)))
            )

    sentinel = object()
    monkeypatch.setattr(
        "intraday_scanner.dashboard.opportunity_projection_store.connect_read_only",
        lambda *_args, **_kwargs: _Connection(),
    )
    monkeypatch.setattr(
        "intraday_scanner.dashboard.opportunity_projection_store.OpportunityStore",
        _Store,
    )
    monkeypatch.setattr(
        "intraday_scanner.dashboard.opportunity_projection_store.build_opportunity_projection",
        lambda _result: sentinel,
    )
    database = tmp_path / "ignored.sqlite"
    database.write_bytes(b"fixture")

    assert (
        load_latest_opportunity_projection(
            database,
            enabled=True,
            expected_market_date="2026-08-11",
        )
        is sentinel
    )


def test_public_projection_manifest_binds_active_lineage(
    tmp_path: Path,
    qualifying_result: PipelineResult,
) -> None:
    projection = build_opportunity_projection(qualifying_result)
    market_date = qualifying_result.decision_at.date().isoformat()
    manifest = write_public_opportunity_projection(
        tmp_path,
        projection,
        expected_market_date=market_date,
    )
    payload = json.loads((tmp_path / "opportunity-projection.json").read_text("utf-8"))

    assert payload["market_date"] == market_date
    assert manifest["market_date"] == market_date
    assert manifest["source_run_id"] == qualifying_result.run_id
    assert manifest["as_of"] == payload["as_of"]
    with pytest.raises(ValueError, match="expected market date"):
        write_public_opportunity_projection(
            tmp_path / "mismatch",
            projection,
            expected_market_date="2026-08-12",
        )


def test_tampered_latest_run_fails_to_data_unavailable(
    tmp_path: Path,
    no_qualifying_result: PipelineResult,
) -> None:
    database = tmp_path / "tampered.sqlite"
    store = OpportunityStore(database)
    store.initialize()
    store.append_run(no_qualifying_result, recorded_at=RECORDED_AT)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER opportunity_pipeline_runs_no_update")
        connection.execute(
            "UPDATE opportunity_pipeline_runs SET result_json = ? WHERE run_id = ?",
            ("{}", no_qualifying_result.run_id),
        )
        connection.commit()

    projection = load_latest_opportunity_projection(database, enabled=True)

    assert projection.state is OpportunityProjectionState.DATA_UNAVAILABLE
    assert projection.reason_code is OpportunityProjectionReason.REPLAY_FAILED
    assert projection.source_run_id is None


def test_verified_no_qualifying_is_the_only_exact_no_trade_state(
    no_qualifying_result: PipelineResult,
) -> None:
    projection = build_opportunity_projection(no_qualifying_result)

    assert projection.state is OpportunityProjectionState.NO_QUALIFYING
    assert projection.message == NO_QUALIFYING_MESSAGE
    assert projection.source_run_id == no_qualifying_result.run_id
    assert unavailable_projection(
        OpportunityProjectionReason.NO_PERSISTED_RUN
    ).message != NO_QUALIFYING_MESSAGE


def test_qualifying_projection_preserves_rank_evidence_and_research_boundary(
    qualifying_result: PipelineResult,
) -> None:
    projection = build_opportunity_projection(qualifying_result)
    encoded = json.loads(projection.to_json())

    assert projection.state is OpportunityProjectionState.QUALIFYING
    assert len(projection.rows) <= 5
    assert [row.rank for row in projection.rows] == [1, 2]
    assert [row.symbol for row in projection.rows] == ["ABC", "DEF"]
    assert all(row.decision == "watch" for row in projection.rows)
    assert all(row.evidence_kind == "heuristic" for row in projection.rows)
    assert all(row.lifecycle == "experimental" for row in projection.rows)
    assert all("not validated" in row.validation_wording.lower() for row in projection.rows)
    assert all(row.triggered_anomalies for row in projection.rows)
    assert all(row.liquidity_score == Decimal("0.5") for row in projection.rows)
    assert encoded["research_only"] is True
    assert encoded["order_execution_enabled"] is False
    assert all(row["order_execution_enabled"] is False for row in encoded["rows"])
    assert "validated statistic" not in projection.to_json().lower()


def test_missing_projection_values_remain_null_and_contract_is_bounded() -> None:
    row = _minimal_row()
    projection = OpportunityProjection(
        state=OpportunityProjectionState.QUALIFYING,
        reason_code=None,
        message="Persisted research only.",
        source_run_id="opportunity-run:000000000000000000000000",
        as_of=NOW,
        rows=(row,),
    )

    payload = json.loads(projection.to_json())
    assert payload["rows"][0]["liquidity_score"] is None
    assert payload["rows"][0]["entry_price"] is None
    assert payload["rows"][0]["invalidation_price"] is None
    assert payload["rows"][0]["target_price"] is None
    with pytest.raises(ValueError, match="row bound"):
        replace(projection, rows=(row,) * 6)
    with pytest.raises(ValueError, match="bounded public text"):
        replace(row, risks=(r"C:\private\state.sqlite",))
    with pytest.raises(ValueError, match="bounded public text"):
        replace(row, why=("<script>alert(1)</script>",))


def test_public_writer_is_deterministic_hash_bound_and_path_free(tmp_path: Path) -> None:
    projection = disabled_projection()
    first_manifest = write_public_opportunity_projection(tmp_path / "first", projection)
    second_manifest = write_public_opportunity_projection(tmp_path / "second", projection)
    first = (tmp_path / "first" / "opportunity-projection.json").read_bytes()
    second = (tmp_path / "second" / "opportunity-projection.json").read_bytes()

    assert first == second
    assert first_manifest == second_manifest
    assert first_manifest["payload_sha256"] == hashlib.sha256(first).hexdigest()
    assert first_manifest["row_count"] == 0
    assert b"C:\\" not in first
    assert b"/Users/" not in first


class _StreamlitRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: object, **kwargs: object) -> None:
            self.calls.append((name, args, kwargs))

        return record


def test_streamlit_renderer_covers_all_states_without_actions(
    qualifying_result: PipelineResult,
) -> None:
    disabled = _StreamlitRecorder()
    render_streamlit_opportunity_projection(disabled, disabled_projection())
    assert disabled.calls == []

    unavailable = _StreamlitRecorder()
    render_streamlit_opportunity_projection(
        unavailable,
        unavailable_projection(OpportunityProjectionReason.NO_PERSISTED_RUN),
    )
    assert any(call[0] == "warning" for call in unavailable.calls)

    no_trade = _StreamlitRecorder()
    render_streamlit_opportunity_projection(
        no_trade,
        OpportunityProjection(
            state=OpportunityProjectionState.NO_QUALIFYING,
            reason_code=None,
            message=NO_QUALIFYING_MESSAGE,
            source_run_id="opportunity-run:000000000000000000000000",
            as_of=NOW,
            rows=(),
        ),
    )
    assert ("info", (NO_QUALIFYING_MESSAGE,), {}) in no_trade.calls

    qualifying = _StreamlitRecorder()
    render_streamlit_opportunity_projection(
        qualifying,
        build_opportunity_projection(qualifying_result),
    )
    rendered = repr(qualifying.calls)
    for label in (
        "Today's Best Opportunities",
        "Lifecycle",
        "Evidence kind",
        "Validation wording",
        "Triggered anomalies",
        "Liquidity",
        "Why",
        "Risks",
        "Vetoes",
        "Entry",
        "Invalidation",
        "Target",
        "Limitations",
        "no TAKE authorization",
    ):
        assert label in rendered
    assert "button" not in {call[0] for call in qualifying.calls}


def _minimal_row() -> OpportunityRowProjection:
    return OpportunityRowProjection(
        rank=1,
        symbol="ABC",
        strategy_id="DS-MOM-001",
        strategy_version="1.0.0",
        direction="long",
        decision="watch",
        lifecycle="experimental",
        evidence_kind="heuristic",
        validation_wording="Experimental research; not validated.",
        market_regime="unknown",
        market_regime_evidence_kind="heuristic",
        security_regime="unknown",
        security_regime_evidence_kind="heuristic",
        triggered_anomalies=(),
        liquidity_score=None,
        liquidity_evidence_kind=None,
        why=(),
        risks=(),
        vetoes=(),
        entry_price=None,
        invalidation_price=None,
        target_price=None,
        limitations=("missing values remain unavailable",),
    )
