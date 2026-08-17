from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner.storage.opportunity_store import (
    _artifact_inventory_hash,
    _build_artifact_inventory,
    _build_persistence_receipt,
)
from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.data_truth.intraday import (
    CorporateActionRecord,
    IntradayBar,
    IntradayCoverageReceipt,
    IntradayCoverageStatus,
    IntradaySourceMetadata,
    MarketStatusInterval,
    PriceAdjustmentBasis,
)
from intraday_scanner.v2.opportunity.models import (
    StrategyDirection,
    TradeDecisionValue,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_resolution import (
    _build_metrics,
    _path_details,
    _resolve_trade_path,
    _touch_interval,
)
from intraday_scanner.v2.opportunity.outcomes import (
    OutcomeCompleteness,
    OutcomeEntryStatus,
    OutcomeHorizonKind,
    OutcomeLabelBatch,
    OutcomeMetric,
    OutcomePathStatus,
    OutcomeRecord,
    OutcomeReferencePriceKind,
    OutcomeValueStatus,
    build_outcome_bar_evidence,
    build_outcome_horizon,
    build_outcome_label_policy,
    build_outcome_observation_dataset,
    build_outcome_observation_series,
    label_pipeline_outcomes,
)
from intraday_scanner.v2.opportunity.pipeline import (
    prepare_opportunity_pipeline,
    run_opportunity_pipeline,
)
from intraday_scanner.v2.opportunity.registry import StrategyRegistry
from tests.test_opportunity_pipeline import (
    _finalized_two_strategy_pipeline,
    _pipeline_dataset,
    _pipeline_risk_policy,
    _pipeline_universe,
)
from tests.test_opportunity_universe_risk import (
    GATE_CONFIG,
    _base_risk_metrics,
    _empirical_evaluation,
    _execution_risk,
    _gate_one,
)

UTC = timezone.utc


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _receipt(result, recorded_at: datetime):
    inventory = _build_artifact_inventory(result)
    return _build_persistence_receipt(
        result,
        inventory=inventory,
        inventory_hash=_artifact_inventory_hash(inventory),
        recorded_at=recorded_at,
    )


def _source_series(result, symbol: str, *, missing_index: int | None = None):
    decision_utc = result.decision_at.astimezone(UTC)
    interval_start = decision_utc + timedelta(seconds=1)
    horizon_end = decision_utc + timedelta(seconds=301)
    fetched_at = horizon_end + timedelta(seconds=1)
    metadata = IntradaySourceMetadata(
        provider="fixture-provider",
        feed="fixture-bars",
        entitlement="fixture-research",
        exchange_session_id="XNYS-2026-08-11",
        request_start=interval_start,
        request_end=horizon_end,
        fetched_at=fetched_at,
        code_sha=_hash("fixture-code"),
        raw_artifact_hash_sha256=_hash(f"raw-{symbol}"),
        normalized_artifact_hash_sha256=_hash(f"normalized-{symbol}"),
        retention_status="retained_fixture",
    )
    observations = []
    for index in range(5):
        start = interval_start + timedelta(minutes=index)
        end = start + timedelta(minutes=1)
        if missing_index == index:
            continue
        open_price = Decimal("100") + Decimal(index) / Decimal("10")
        bar = IntradayBar(
            symbol=symbol,
            exchange_session_id="XNYS-2026-08-11",
            timestamp=end,
            open_price=open_price,
            high_price=open_price + Decimal("0.20"),
            low_price=open_price - Decimal("0.20"),
            close_price=open_price + Decimal("0.10"),
            volume=1000 + index,
            vwap=open_price + Decimal("0.05"),
            price_adjustment_basis=PriceAdjustmentBasis.UNADJUSTED,
            source_metadata=metadata,
            trade_count=100 + index,
        )
        observations.append(
            build_outcome_bar_evidence(
                bar=bar,
                interval_start_at=start,
                interval_end_at=end,
                available_at=fetched_at,
            )
        )
    coverage = IntradayCoverageReceipt(
        coverage_receipt_id=f"coverage-{symbol.lower()}",
        provider="fixture-provider",
        feed="fixture-bars",
        entitlement="fixture-research",
        symbol=symbol,
        market_date="2026-08-11",
        exchange_session_id="XNYS-2026-08-11",
        request_start=interval_start,
        request_end=horizon_end,
        status=(
            IntradayCoverageStatus.PARTIAL_MISSING_INTERVALS
            if missing_index is not None
            else IntradayCoverageStatus.COMPLETE
        ),
        source_metadata=metadata,
        observed_start=observations[0].interval_start_at,
        observed_end=horizon_end,
        missing_intervals=(
            (
                (
                    interval_start + timedelta(minutes=missing_index),
                    interval_start + timedelta(minutes=missing_index + 1),
                ),
            )
            if missing_index is not None
            else ()
        ),
        artifact_manifest_ids=(f"manifest-{symbol.lower()}",),
        reason="fixture_coverage",
        created_at=fetched_at,
    )
    return build_outcome_observation_series(
        symbol=symbol,
        exchange_session_id="XNYS-2026-08-11",
        decision_at=result.decision_at,
        requested_through_at=horizon_end,
        coverage_receipt=coverage,
        observations=tuple(observations),
        source_identity="fixture_outcome_source",
        method="retained_post_decision_minute_bars",
    )


def _batch(*, missing_symbol: str | None = None, missing_index: int = 2):
    _prepared, _risks, result = _finalized_two_strategy_pipeline()
    decision_utc = result.decision_at.astimezone(UTC)
    horizon = build_outcome_horizon(
        decision_at=result.decision_at,
        exchange_session_id="XNYS-2026-08-11",
        session_open_at=decision_utc - timedelta(minutes=4),
        session_close_at=decision_utc + timedelta(hours=6, minutes=26),
        kind=OutcomeHorizonKind.ELAPSED_SECONDS,
        elapsed_seconds=301,
    )
    series = tuple(
        _source_series(
            result,
            symbol,
            missing_index=missing_index if symbol == missing_symbol else None,
        )
        for symbol in ("ABC", "DEF")
    )
    frozen_at = horizon.end_at + timedelta(seconds=1)
    dataset = build_outcome_observation_dataset(
        decision_at=result.decision_at,
        frozen_at=frozen_at,
        series=series,
    )
    policy = build_outcome_label_policy(
        policy_version="wp003-b-v1",
        expected_bar_interval_seconds=60,
    )
    batch = label_pipeline_outcomes(
        pipeline_result=result,
        persistence_receipt=_receipt(result, decision_utc),
        source_dataset=dataset,
        policy=policy,
        horizons=(horizon,),
        recorded_at=frozen_at,
    )
    return batch


def _path_observations(decision, bars: tuple[tuple[str, str, str, str], ...]):
    decision_utc = decision.decision_at.astimezone(UTC)
    start_at = decision_utc + timedelta(seconds=1)
    end_at = start_at + timedelta(minutes=len(bars))
    metadata = IntradaySourceMetadata(
        provider="fixture-provider",
        feed="fixture-bars",
        entitlement="fixture-research",
        exchange_session_id="XNYS-2026-08-11",
        request_start=start_at,
        request_end=end_at,
        fetched_at=end_at,
        code_sha=_hash("path-code"),
        raw_artifact_hash_sha256=_hash("path-raw"),
        normalized_artifact_hash_sha256=_hash("path-normalized"),
        retention_status="retained_fixture",
    )
    observations = []
    for index, (open_value, high_value, low_value, close_value) in enumerate(bars):
        start = start_at + timedelta(minutes=index)
        end = start + timedelta(minutes=1)
        bar = IntradayBar(
            symbol=decision.symbol,
            exchange_session_id="XNYS-2026-08-11",
            timestamp=end,
            open_price=Decimal(open_value),
            high_price=Decimal(high_value),
            low_price=Decimal(low_value),
            close_price=Decimal(close_value),
            volume=1000,
            vwap=Decimal(close_value),
            price_adjustment_basis=PriceAdjustmentBasis.UNADJUSTED,
            source_metadata=metadata,
        )
        observations.append(
            build_outcome_bar_evidence(
                bar=bar,
                interval_start_at=start,
                interval_end_at=end,
            )
        )
    return tuple(observations)


def _pending_terminal_batch():
    _prepared, _risks, result = _finalized_two_strategy_pipeline()
    decision_utc = result.decision_at.astimezone(UTC)
    start_at = decision_utc + timedelta(seconds=1)
    observed_end = start_at + timedelta(minutes=2)
    frozen_at = observed_end + timedelta(seconds=1)
    horizon = build_outcome_horizon(
        decision_at=result.decision_at,
        exchange_session_id="XNYS-2026-08-11",
        session_open_at=decision_utc - timedelta(minutes=4),
        session_close_at=decision_utc + timedelta(hours=6, minutes=26),
        kind=OutcomeHorizonKind.ELAPSED_SECONDS,
        elapsed_seconds=301,
    )
    series_items = []
    for symbol in ("ABC", "DEF"):
        metadata = IntradaySourceMetadata(
            provider="fixture-provider",
            feed="fixture-bars",
            entitlement="fixture-research",
            exchange_session_id="XNYS-2026-08-11",
            request_start=start_at,
            request_end=observed_end,
            fetched_at=frozen_at,
            code_sha=_hash("pending-code"),
            raw_artifact_hash_sha256=_hash(f"pending-raw-{symbol}"),
            normalized_artifact_hash_sha256=_hash(f"pending-normalized-{symbol}"),
            retention_status="retained_fixture",
        )
        bars = (
            (Decimal("104"), Decimal("104.4"), Decimal("103.8"), Decimal("104.1")),
            (Decimal("104.2"), Decimal("105.3"), Decimal("104"), Decimal("105.2")),
        )
        observations = []
        for index, (open_price, high_price, low_price, close_price) in enumerate(bars):
            interval_start = start_at + timedelta(minutes=index)
            interval_end = interval_start + timedelta(minutes=1)
            observations.append(
                build_outcome_bar_evidence(
                    bar=IntradayBar(
                        symbol=symbol,
                        exchange_session_id="XNYS-2026-08-11",
                        timestamp=interval_end,
                        open_price=open_price,
                        high_price=high_price,
                        low_price=low_price,
                        close_price=close_price,
                        volume=1000,
                        vwap=close_price,
                        price_adjustment_basis=PriceAdjustmentBasis.UNADJUSTED,
                        source_metadata=metadata,
                    ),
                    interval_start_at=interval_start,
                    interval_end_at=interval_end,
                )
            )
        coverage = IntradayCoverageReceipt(
            coverage_receipt_id=f"pending-coverage-{symbol.lower()}",
            provider=metadata.provider,
            feed=metadata.feed,
            entitlement=metadata.entitlement,
            symbol=symbol,
            market_date="2026-08-11",
            exchange_session_id=metadata.exchange_session_id,
            request_start=start_at,
            request_end=observed_end,
            status=IntradayCoverageStatus.COMPLETE,
            source_metadata=metadata,
            observed_start=start_at,
            observed_end=observed_end,
            reason="horizon_still_open",
            created_at=frozen_at,
        )
        series_items.append(
            build_outcome_observation_series(
                symbol=symbol,
                exchange_session_id=metadata.exchange_session_id,
                decision_at=result.decision_at,
                requested_through_at=observed_end,
                coverage_receipt=coverage,
                observations=tuple(observations),
                source_identity="pending_fixture_source",
                method="retained_partial_minute_prefix",
            )
        )
    dataset = build_outcome_observation_dataset(
        decision_at=result.decision_at,
        frozen_at=frozen_at,
        series=tuple(series_items),
    )
    batch = label_pipeline_outcomes(
        pipeline_result=result,
        persistence_receipt=_receipt(result, decision_utc),
        source_dataset=dataset,
        policy=build_outcome_label_policy(
            policy_version="wp003-b-v1",
            expected_bar_interval_seconds=60,
        ),
        horizons=(horizon,),
        recorded_at=frozen_at,
    )
    return batch


def _relabel_with_series(batch, series):
    dataset = build_outcome_observation_dataset(
        decision_at=batch.pipeline_result.decision_at,
        frozen_at=batch.source_dataset.frozen_at,
        series=series,
        limitations=batch.source_dataset.limitations,
    )
    return label_pipeline_outcomes(
        pipeline_result=batch.pipeline_result,
        persistence_receipt=batch.persistence_receipt,
        source_dataset=dataset,
        policy=batch.policy,
        horizons=batch.horizons,
        recorded_at=batch.recorded_at,
    )


def _rebuild_series(
    series,
    *,
    coverage=None,
    statuses=(),
    actions=None,
    observations=None,
):
    return build_outcome_observation_series(
        symbol=series.symbol,
        exchange_session_id=series.exchange_session_id,
        decision_at=series.decision_at,
        requested_through_at=series.requested_through_at,
        first_expected_interval_start_at=series.first_expected_interval_start_at,
        coverage_receipt=coverage or series.coverage_receipt,
        observations=series.observations if observations is None else observations,
        market_status_intervals=statuses,
        corporate_actions=series.corporate_actions if actions is None else actions,
        source_identity=series.source_identity,
        method=series.method,
        limitations=series.limitations,
    )


def _replace_observation_prices(
    observation,
    *,
    open_price: str,
    high_price: str,
    low_price: str,
    close_price: str,
):
    bar = replace(
        observation.bar,
        open_price=Decimal(open_price),
        high_price=Decimal(high_price),
        low_price=Decimal(low_price),
        close_price=Decimal(close_price),
        vwap=Decimal(close_price),
    )
    return build_outcome_bar_evidence(
        bar=bar,
        interval_start_at=observation.interval_start_at,
        interval_end_at=observation.interval_end_at,
    )


def _rehash_record_payload(payload):
    payload["outcome_id"] = stable_identity(
        "opportunity-outcome",
        {key: value for key, value in payload.items() if key != "outcome_id"},
    )
    return payload


def _assert_record_and_batch_payload_rejected(
    batch,
    record_index: int,
    mutate,
    match: str,
) -> None:
    record_payload = batch.outcomes[record_index].to_dict()
    mutate(record_payload)
    _rehash_record_payload(record_payload)
    with pytest.raises(ValueError, match=match):
        OutcomeRecord.from_dict(record_payload)
    with pytest.raises(ValueError, match=match):
        OutcomeRecord.from_json(contract_to_json(record_payload))

    batch_payload = batch.to_dict()
    batch_payload["outcomes"][record_index] = record_payload
    batch_payload["batch_id"] = stable_identity(
        "outcome-label-batch",
        {key: value for key, value in batch_payload.items() if key != "batch_id"},
    )
    with pytest.raises(ValueError, match=match):
        OutcomeLabelBatch.from_dict(batch_payload)
    with pytest.raises(ValueError, match=match):
        OutcomeLabelBatch.from_json(contract_to_json(batch_payload))


def _complete_horizon_exit_batch():
    batch = _batch()
    first = batch.source_dataset.series[0]
    prices = (
        ("104", "104.2", "103.8", "104.1"),
        ("104.1", "104.4", "103.8", "104.2"),
        ("104.2", "104.5", "103.9", "104.3"),
        ("104.3", "104.6", "104.0", "104.4"),
        ("104.4", "104.7", "104.1", "104.5"),
    )
    observations = tuple(
        _replace_observation_prices(
            observation,
            open_price=values[0],
            high_price=values[1],
            low_price=values[2],
            close_price=values[3],
        )
        for observation, values in zip(first.observations, prices, strict=True)
    )
    rebuilt = _rebuild_series(first, observations=observations)
    return _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))


def _complete_target_first_batch():
    batch = _batch()
    first = batch.source_dataset.series[0]
    prices = (
        ("104", "104.2", "103.8", "104.1"),
        ("104.2", "105.3", "104.0", "105.2"),
        ("105.0", "105.1", "104.5", "104.9"),
        ("104.9", "105.0", "104.4", "104.8"),
        ("104.8", "104.9", "104.3", "104.7"),
    )
    observations = tuple(
        _replace_observation_prices(
            observation,
            open_price=values[0],
            high_price=values[1],
            low_price=values[2],
            close_price=values[3],
        )
        for observation, values in zip(first.observations, prices, strict=True)
    )
    rebuilt = _rebuild_series(first, observations=observations)
    return _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))


def test_outcomes_are_structurally_isolated_from_realtime_package_imports() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import intraday_scanner.v2.opportunity; "
                "import intraday_scanner.v2.opportunity.models; "
                "import intraday_scanner.v2.opportunity.features; "
                "import intraday_scanner.v2.opportunity.discovery; "
                "import intraday_scanner.v2.opportunity.regimes; "
                "import intraday_scanner.v2.opportunity.registry; "
                "import intraday_scanner.v2.opportunity.ranking; "
                "import intraday_scanner.v2.opportunity.risk; "
                "import intraday_scanner.v2.opportunity.quality_gate; "
                "import intraday_scanner.v2.opportunity.pipeline; "
                "import intraday_scanner.storage.opportunity_store; "
                "import intraday_scanner.v2.opportunity as package; "
                "import intraday_scanner.v2.opportunity.models as models; "
                "assert not any(name.startswith('intraday_scanner.v2.opportunity.outcome') "
                "for name in sys.modules); "
                "assert not hasattr(models, 'OutcomeRecord'); "
                "assert not hasattr(package, 'OutcomeRecord')"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    forbidden = {
        "alpha.path_replay",
        "backtest",
        "app",
        "broker",
        "network",
        "runtime",
        "streamlit",
    }
    for outcome_path in Path("intraday_scanner/v2/opportunity").glob("outcome*.py"):
        tree = ast.parse(outcome_path.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert not any(any(part in name for part in forbidden) for name in imported), outcome_path


def test_batch_labels_exact_evaluation_horizon_product_and_round_trips() -> None:
    batch = _batch()
    assert len(batch.outcomes) == len(batch.pipeline_result.evaluations) == 4
    assert tuple(item.evaluation_id for item in batch.outcomes) == tuple(
        item.evaluation_id for item in batch.pipeline_result.evaluations
    )
    assert all(item.completeness is OutcomeCompleteness.COMPLETE for item in batch.outcomes)
    assert all(item.promotion_eligible is False for item in batch.outcomes)
    assert all(item.retrospective_research_only is True for item in batch.outcomes)
    assert OutcomeLabelBatch.from_json(batch.to_json()) == batch


def test_pass_and_insufficient_decisions_keep_counterfactual_invalidation_truth() -> None:
    batch = _batch()
    long_records = [item for item in batch.outcomes if item.direction is StrategyDirection.LONG]
    assert all(item.entry_status is OutcomeEntryStatus.UNATTAINABLE for item in long_records)
    assert all(item.path_status is OutcomePathStatus.UNATTAINABLE_FILL for item in long_records)
    for record in long_records:
        metric = next(
            item for item in record.metrics if item.metric is OutcomeMetric.REFERENCE_HORIZON_RETURN
        )
        assert metric.status is OutcomeValueStatus.DERIVED
        assert metric.value is not None
        assert record.reference_price_kind is OutcomeReferencePriceKind.FIRST_POST_DECISION_OPEN
        assert record.modeled_entry_price is None


def test_missing_initial_interval_is_representable_and_never_complete() -> None:
    batch = _batch(missing_symbol="ABC", missing_index=0)
    affected = [item for item in batch.outcomes if item.symbol == "ABC"]
    assert all(item.completeness is OutcomeCompleteness.CENSORED for item in affected)
    assert all(item.path_status is OutcomePathStatus.MISSING_BARS for item in affected)
    assert all("missing_initial_post_decision_interval" in item.reasons for item in affected)


def test_missing_interval_censors_only_affected_symbol_horizon() -> None:
    batch = _batch(missing_symbol="ABC")
    abc = [item for item in batch.outcomes if item.symbol == "ABC"]
    other = [item for item in batch.outcomes if item.symbol == "DEF"]
    assert all(item.completeness is OutcomeCompleteness.CENSORED for item in abc)
    assert all(item.path_status is OutcomePathStatus.MISSING_BARS for item in abc)
    assert all(item.completeness is OutcomeCompleteness.COMPLETE for item in other)
    assert OutcomeLabelBatch.from_json(batch.to_json()) == batch


def test_direct_batch_rejects_cross_source_and_metric_tamper() -> None:
    batch = _batch()
    first = batch.outcomes[0]
    second = batch.outcomes[2]
    forged_payload = first.to_dict()
    forged_payload["source_series_id"] = second.source_series_id
    forged_payload["source_series_content_hash_sha256"] = (
        second.source_series_content_hash_sha256
    )
    forged_payload["outcome_id"] = stable_identity(
        "opportunity-outcome",
        {key: value for key, value in forged_payload.items() if key != "outcome_id"},
    )
    with pytest.raises(ValueError, match="source series binding"):
        OutcomeRecord.from_dict(forged_payload)
    metric = first.metrics[0]
    with pytest.raises(ValueError, match="derived-only"):
        replace(metric, status=OutcomeValueStatus.OBSERVED)


def test_standalone_record_rejects_reference_price_not_bound_to_embedded_bar() -> None:
    batch = _pending_terminal_batch()
    record_index = next(
        index
        for index, record in enumerate(batch.outcomes)
        if record.path_status is OutcomePathStatus.TARGET_FIRST
    )

    def mutate(payload) -> None:
        payload["reference_price"] = "999"

    _assert_record_and_batch_payload_rejected(
        batch,
        record_index,
        mutate,
        "reference price does not match first source observation open",
    )


def test_standalone_record_rejects_shifted_touch_interval_and_time_metric() -> None:
    batch = _pending_terminal_batch()
    record_index = next(
        index
        for index, record in enumerate(batch.outcomes)
        if record.path_status is OutcomePathStatus.TARGET_FIRST
    )

    def mutate(payload) -> None:
        entry = payload["entry_interval"]
        entry["interval_start_at"] = (
            datetime.fromisoformat(entry["interval_start_at"]) + timedelta(seconds=1)
        ).isoformat()
        upper = next(
            item
            for item in payload["metrics"]
            if item["metric"] == OutcomeMetric.TIME_TO_TARGET_UPPER_BOUND.value
        )
        upper["value"] = str(Decimal(upper["value"]) - Decimal("1"))

    _assert_record_and_batch_payload_rejected(
        batch,
        record_index,
        mutate,
        "touch interval does not match embedded observation",
    )


def test_standalone_record_rejects_excursions_not_recomputed_from_embedded_bars() -> None:
    batch = _complete_horizon_exit_batch()
    record_index = next(
        index
        for index, record in enumerate(batch.outcomes)
        if record.path_status is OutcomePathStatus.HORIZON_EXIT
    )

    def mutate(payload) -> None:
        values = {
            OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R.value: "99",
            OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R.value: "-99",
        }
        for metric in payload["metrics"]:
            if metric["metric"] in values:
                metric["value"] = values[metric["metric"]]

    _assert_record_and_batch_payload_rejected(
        batch,
        record_index,
        mutate,
        "outcome metrics do not match embedded source evidence",
    )


def test_standalone_record_rejects_complete_horizon_exit_relabel_as_missing() -> None:
    batch = _complete_horizon_exit_batch()
    record_index = next(
        index
        for index, record in enumerate(batch.outcomes)
        if record.path_status is OutcomePathStatus.HORIZON_EXIT
    )

    def mutate(payload) -> None:
        payload["completeness"] = OutcomeCompleteness.CENSORED.value
        payload["entry_status"] = OutcomeEntryStatus.UNSUPPORTED.value
        payload["path_status"] = OutcomePathStatus.MISSING_BARS.value
        payload["reasons"] = ["bars_do_not_reach_horizon_end"]
        for field in (
            "modeled_entry_price",
            "modeled_exit_price",
            "entry_interval",
            "exit_interval",
            "target_touch_interval",
            "stop_touch_interval",
        ):
            payload[field] = None
        for metric in payload["metrics"]:
            if metric["metric"] == OutcomeMetric.REFERENCE_HORIZON_RETURN.value:
                continue
            metric.update(
                value=None,
                status=OutcomeValueStatus.UNAVAILABLE.value,
                observed_at=None,
                source_observation_ids=[],
                method="outcome_metric_unavailable",
                reason="resolved_filled_path_required",
            )

    _assert_record_and_batch_payload_rejected(
        batch,
        record_index,
        mutate,
        "completeness, entry status, and path status",
    )


def test_standalone_record_rejects_complete_target_relabel_as_partial() -> None:
    batch = _complete_target_first_batch()
    record_index = next(
        index
        for index, record in enumerate(batch.outcomes)
        if record.path_status is OutcomePathStatus.TARGET_FIRST
    )

    def mutate(payload) -> None:
        payload["completeness"] = OutcomeCompleteness.PARTIAL.value
        payload["reasons"] = ["terminal_path_resolved_before_horizon_end"]

    _assert_record_and_batch_payload_rejected(
        batch,
        record_index,
        mutate,
        "completeness, entry status, and path status",
    )


def test_pass_and_insufficient_geometry_receive_counterfactual_terminal_paths() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    passing_risk_but_score_fail = replace(
        GATE_CONFIG,
        minimum_take_score=Decimal("1"),
    )
    pass_decision = _gate_one(evaluation, risk, config=passing_risk_but_score_fail)
    insufficient_decision = _gate_one(evaluation, None, config=GATE_CONFIG)
    observations = _path_observations(
        pass_decision,
        (
            ("100", "101", "99.5", "100.4"),
            ("101", "106.2", "100.5", "106"),
        ),
    )
    for decision in (pass_decision, insufficient_decision):
        entry_status, path_status = _resolve_trade_path(decision, observations)
        assert entry_status is OutcomeEntryStatus.FILLED
        assert path_status is OutcomePathStatus.TARGET_FIRST


def test_partial_terminal_path_retains_metrics_and_interval_uncertainty() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    decision = _gate_one(evaluation, risk)
    observations = _path_observations(
        decision,
        (
            ("100", "101", "99.5", "100.4"),
            ("101", "106.2", "100.5", "106"),
        ),
    )
    entry_status, path_status = _resolve_trade_path(decision, observations)
    path = _path_details(decision, observations, entry_status, path_status)
    metrics = {
        item.metric: item
        for item in _build_metrics(
            decision=decision,
            risk=risk,
            reference=observations[0],
            close_observation=None,
            observations=observations,
            completeness=OutcomeCompleteness.PARTIAL,
            entry_status=entry_status,
            path_status=path_status,
            path=path,
        )
    }
    assert metrics[OutcomeMetric.REFERENCE_HORIZON_RETURN].status is OutcomeValueStatus.UNAVAILABLE
    assert metrics[OutcomeMetric.SIMULATED_GROSS_R].value == Decimal("3")
    assert metrics[OutcomeMetric.SIMULATED_AFTER_COST_R].value == Decimal("578") / Decimal("222")
    assert metrics[OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R].value == Decimal("3")
    assert metrics[OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R].value == Decimal("0")
    assert metrics[OutcomeMetric.TIME_TO_TARGET_LOWER_BOUND].value == Decimal("0")
    assert metrics[OutcomeMetric.TIME_TO_TARGET_UPPER_BOUND].value == Decimal("120")
    assert metrics[OutcomeMetric.TIME_TO_TARGET_LOWER_BOUND].source_observation_ids == (
        observations[0].observation_id,
        observations[1].observation_id,
    )


def test_public_batch_preserves_terminal_prefix_while_horizon_is_pending() -> None:
    batch = _pending_terminal_batch()
    eligible = [item for item in batch.outcomes if item.direction is StrategyDirection.LONG]
    noneligible = [item for item in batch.outcomes if item.direction is StrategyDirection.BOTH]
    assert all(item.completeness is OutcomeCompleteness.PARTIAL for item in eligible)
    assert all(item.entry_status is OutcomeEntryStatus.FILLED for item in eligible)
    assert all(item.path_status is OutcomePathStatus.TARGET_FIRST for item in eligible)
    for record in eligible:
        metrics = {item.metric: item for item in record.metrics}
        assert metrics[OutcomeMetric.REFERENCE_HORIZON_RETURN].status is (
            OutcomeValueStatus.UNAVAILABLE
        )
        assert metrics[OutcomeMetric.SIMULATED_GROSS_R].value == Decimal("2")
        assert metrics[OutcomeMetric.SIMULATED_AFTER_COST_R].value is not None
        assert metrics[OutcomeMetric.TIME_TO_TARGET_UPPER_BOUND].value == Decimal("120")
        assert record.target_touch_interval is not None
        assert record.target_touch_interval.observation_id == record.source_observation_ids[1]
    assert all(item.completeness is OutcomeCompleteness.PENDING for item in noneligible)
    assert all(item.path_status is OutcomePathStatus.PENDING_HORIZON for item in noneligible)
    assert OutcomeLabelBatch.from_json(batch.to_json()) == batch


def test_entry_on_final_bar_and_inverted_geometry_are_censored_or_unattainable() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    decision = _gate_one(evaluation, risk)
    final_entry = _path_observations(
        decision,
        (("100", "101", "99.5", "100.4"),),
    )
    assert _resolve_trade_path(decision, final_entry) == (
        OutcomeEntryStatus.ENTRY_BAR_AMBIGUOUS,
        OutcomePathStatus.ENTRY_BAR_AMBIGUOUS,
    )
    inverted_long = SimpleNamespace(
        direction=StrategyDirection.LONG,
        evaluation=replace(
            evaluation,
            invalidation_price=Decimal("101"),
            target_price=Decimal("99"),
        ),
    )
    inverted_short = SimpleNamespace(
        direction=StrategyDirection.SHORT,
        evaluation=replace(
            evaluation,
            direction=StrategyDirection.SHORT,
            invalidation_price=Decimal("98"),
            target_price=Decimal("106"),
        ),
    )
    assert _resolve_trade_path(inverted_long, final_entry) == (
        OutcomeEntryStatus.UNATTAINABLE,
        OutcomePathStatus.UNATTAINABLE_FILL,
    )
    assert _resolve_trade_path(inverted_short, final_entry) == (
        OutcomeEntryStatus.UNATTAINABLE,
        OutcomePathStatus.UNATTAINABLE_FILL,
    )


@pytest.mark.parametrize("status,halts", [("open", False), ("halted", True)])
def test_market_status_taxonomy_distinguishes_halts_from_nonhalts(
    status: str,
    halts: bool,
) -> None:
    batch = _batch()
    first = batch.source_dataset.series[0]
    source = first.observations[0].bar.source_metadata
    dynamic_status = "".join(status)
    interval = MarketStatusInterval(
        symbol=first.symbol,
        exchange_session_id=first.exchange_session_id,
        status=dynamic_status,
        start=first.observations[0].interval_start_at,
        end=first.observations[0].interval_end_at,
        reason="fixture_market_status",
        source_metadata=source,
    )
    rebuilt = _rebuild_series(first, statuses=(interval,))
    labeled = _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))
    affected = [item for item in labeled.outcomes if item.symbol == "ABC"]
    if halts:
        assert all(item.path_status is OutcomePathStatus.HALT_CENSORED for item in affected)
        assert all(item.completeness is OutcomeCompleteness.CENSORED for item in affected)
    else:
        assert all(item.path_status is not OutcomePathStatus.HALT_CENSORED for item in affected)
        assert all(item.completeness is OutcomeCompleteness.COMPLETE for item in affected)
    assert OutcomeLabelBatch.from_json(labeled.to_json()) == labeled


def test_known_halt_coverage_receipt_is_halt_censor_proof() -> None:
    batch = _batch(missing_symbol="ABC")
    first = batch.source_dataset.series[0]
    coverage = replace(
        first.coverage_receipt,
        status=IntradayCoverageStatus.KNOWN_HALT_GAPS,
    )
    rebuilt = _rebuild_series(first, coverage=coverage)
    labeled = _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))
    affected = [item for item in labeled.outcomes if item.symbol == "ABC"]
    assert all(item.path_status is OutcomePathStatus.HALT_CENSORED for item in affected)
    assert all("coverage_receipt_reports_known_halt_gaps" in item.reasons for item in affected)


def test_known_halt_gap_censors_only_overlapping_horizon() -> None:
    batch = _batch(missing_symbol="ABC", missing_index=2)
    first = batch.source_dataset.series[0]
    coverage = replace(
        first.coverage_receipt,
        status=IntradayCoverageStatus.KNOWN_HALT_GAPS,
    )
    rebuilt = _rebuild_series(first, coverage=coverage)
    early = build_outcome_horizon(
        decision_at=batch.pipeline_result.decision_at,
        exchange_session_id=first.exchange_session_id,
        session_open_at=batch.horizons[0].session_open_at,
        session_close_at=batch.horizons[0].session_close_at,
        kind=OutcomeHorizonKind.ELAPSED_SECONDS,
        elapsed_seconds=121,
    )
    labeled = label_pipeline_outcomes(
        pipeline_result=batch.pipeline_result,
        persistence_receipt=batch.persistence_receipt,
        source_dataset=build_outcome_observation_dataset(
            decision_at=batch.pipeline_result.decision_at,
            frozen_at=batch.source_dataset.frozen_at,
            series=(rebuilt, batch.source_dataset.series[1]),
        ),
        policy=batch.policy,
        horizons=(early, batch.horizons[0]),
        recorded_at=batch.recorded_at,
    )
    early_records = [
        item
        for item in labeled.outcomes
        if item.symbol == "ABC" and item.horizon_id == early.horizon_id
    ]
    later_records = [
        item
        for item in labeled.outcomes
        if item.symbol == "ABC" and item.horizon_id == batch.horizons[0].horizon_id
    ]
    assert all(item.completeness is OutcomeCompleteness.COMPLETE for item in early_records)
    assert all(item.path_status is not OutcomePathStatus.HALT_CENSORED for item in early_records)
    assert all(item.completeness is OutcomeCompleteness.CENSORED for item in later_records)
    assert all(item.path_status is OutcomePathStatus.HALT_CENSORED for item in later_records)
    assert OutcomeLabelBatch.from_json(labeled.to_json()) == labeled


@pytest.mark.parametrize(
    "status",
    [
        IntradayCoverageStatus.NO_DATA,
        IntradayCoverageStatus.ENTITLEMENT_DENIED,
        IntradayCoverageStatus.HASH_MISMATCH,
        IntradayCoverageStatus.FUTURE_DATA_REJECTED,
        IntradayCoverageStatus.DATA_INELIGIBLE,
    ],
)
def test_hard_unavailable_coverage_cannot_carry_usable_bars(
    status: IntradayCoverageStatus,
) -> None:
    batch = _batch()
    first = batch.source_dataset.series[0]
    contradictory = replace(first.coverage_receipt, status=status)
    with pytest.raises(ValueError, match="hard-unavailable coverage"):
        _rebuild_series(first, coverage=contradictory)


def test_source_availability_provider_vwap_and_trade_count_fail_closed() -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    observation = series.observations[0]
    bar = observation.bar
    too_early = replace(
        bar.source_metadata,
        fetched_at=observation.interval_end_at - timedelta(microseconds=1),
    )
    with pytest.raises(ValueError, match="cannot be fetched before"):
        build_outcome_bar_evidence(
            bar=replace(bar, source_metadata=too_early),
            interval_start_at=observation.interval_start_at,
            interval_end_at=observation.interval_end_at,
        )
    with pytest.raises(ValueError, match="VWAP"):
        build_outcome_bar_evidence(
            bar=replace(bar, vwap=bar.high_price + Decimal("0.01")),
            interval_start_at=observation.interval_start_at,
            interval_end_at=observation.interval_end_at,
        )
    with pytest.raises(ValueError, match="trade_count"):
        build_outcome_bar_evidence(
            bar=replace(bar, trade_count=-1),
            interval_start_at=observation.interval_start_at,
            interval_end_at=observation.interval_end_at,
        )
    foreign_metadata = replace(bar.source_metadata, provider="foreign-provider")
    foreign_observation = build_outcome_bar_evidence(
        bar=replace(bar, source_metadata=foreign_metadata),
        interval_start_at=observation.interval_start_at,
        interval_end_at=observation.interval_end_at,
    )
    with pytest.raises(ValueError, match="does not match series scope"):
        _rebuild_series(
            series,
            observations=(foreign_observation, *series.observations[1:]),
        )


@pytest.mark.parametrize("mixed", [False, True])
def test_unknown_or_mixed_adjustment_bases_never_produce_metrics(mixed: bool) -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    changed = []
    for index, observation in enumerate(series.observations):
        basis = (
            PriceAdjustmentBasis.SPLIT_ADJUSTED
            if mixed and index == 0
            else (
                PriceAdjustmentBasis.UNKNOWN
                if not mixed
                else PriceAdjustmentBasis.UNADJUSTED
            )
        )
        changed.append(
            build_outcome_bar_evidence(
                bar=replace(observation.bar, price_adjustment_basis=basis),
                interval_start_at=observation.interval_start_at,
                interval_end_at=observation.interval_end_at,
            )
        )
    rebuilt = _rebuild_series(series, observations=tuple(changed))
    labeled = _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))
    affected = [item for item in labeled.outcomes if item.symbol == "ABC"]
    assert all(item.completeness is OutcomeCompleteness.UNAVAILABLE for item in affected)
    assert all(item.path_status is OutcomePathStatus.UNSUPPORTED_EVIDENCE for item in affected)
    assert all(all(metric.value is None for metric in item.metrics) for item in affected)


def test_batch_rejects_session_receipt_time_and_secret_limitation_tamper() -> None:
    batch = _batch()
    horizon = build_outcome_horizon(
        decision_at=batch.pipeline_result.decision_at,
        exchange_session_id="XNAS-2026-08-11",
        session_open_at=batch.horizons[0].session_open_at,
        session_close_at=batch.horizons[0].session_close_at,
        kind=batch.horizons[0].kind,
        elapsed_seconds=batch.horizons[0].elapsed_seconds,
    )
    with pytest.raises(ValueError, match="horizon session"):
        label_pipeline_outcomes(
            pipeline_result=batch.pipeline_result,
            persistence_receipt=batch.persistence_receipt,
            source_dataset=batch.source_dataset,
            policy=batch.policy,
            horizons=(horizon,),
            recorded_at=batch.recorded_at,
        )
    late_receipt = _receipt(
        batch.pipeline_result,
        batch.recorded_at + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="cannot precede persistence receipt"):
        label_pipeline_outcomes(
            pipeline_result=batch.pipeline_result,
            persistence_receipt=late_receipt,
            source_dataset=batch.source_dataset,
            policy=batch.policy,
            horizons=batch.horizons,
            recorded_at=batch.recorded_at,
        )
    with pytest.raises(ValueError, match="sanitized"):
        label_pipeline_outcomes(
            pipeline_result=batch.pipeline_result,
            persistence_receipt=batch.persistence_receipt,
            source_dataset=batch.source_dataset,
            policy=batch.policy,
            horizons=batch.horizons,
            recorded_at=batch.recorded_at,
            limitations=("token=private",),
        )


def test_trade_path_ambiguity_does_not_erase_reference_horizon_return() -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    ambiguous = _replace_observation_prices(
        series.observations[0],
        open_price="104",
        high_price="105.3",
        low_price="103.8",
        close_price="104.5",
    )
    rebuilt = _rebuild_series(
        series,
        observations=(ambiguous, *series.observations[1:]),
    )
    labeled = _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))
    record = next(
        item
        for item in labeled.outcomes
        if item.symbol == "ABC" and item.direction is StrategyDirection.LONG
    )
    assert record.completeness is OutcomeCompleteness.CENSORED
    assert record.path_status is OutcomePathStatus.ENTRY_BAR_AMBIGUOUS
    reference = next(
        item for item in record.metrics if item.metric is OutcomeMetric.REFERENCE_HORIZON_RETURN
    )
    assert reference.status is OutcomeValueStatus.DERIVED
    assert reference.value is not None


@pytest.mark.parametrize("gap_after_terminal", [False, True])
def test_missing_bar_order_preserves_only_pre_gap_terminal_path(
    gap_after_terminal: bool,
) -> None:
    missing_index = 2 if gap_after_terminal else 1
    batch = _batch(missing_symbol="ABC", missing_index=missing_index)
    series = batch.source_dataset.series[0]
    observations = list(series.observations)
    observations[0] = _replace_observation_prices(
        observations[0],
        open_price="104",
        high_price="104.4",
        low_price="103.8",
        close_price="104.1",
    )
    target_index = 1 if gap_after_terminal else 2
    observations[target_index] = _replace_observation_prices(
        observations[target_index],
        open_price="104",
        high_price="105.3",
        low_price="104",
        close_price="105.2",
    )
    rebuilt = _rebuild_series(series, observations=tuple(observations))
    labeled = _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))
    record = next(
        item
        for item in labeled.outcomes
        if item.symbol == "ABC" and item.direction is StrategyDirection.LONG
    )
    if gap_after_terminal:
        assert record.completeness is OutcomeCompleteness.PARTIAL
        assert record.path_status is OutcomePathStatus.TARGET_FIRST
        assert next(
            item for item in record.metrics if item.metric is OutcomeMetric.SIMULATED_GROSS_R
        ).value == Decimal("2")
    else:
        assert record.completeness is OutcomeCompleteness.CENSORED
        assert record.path_status is OutcomePathStatus.MISSING_BARS


@pytest.mark.parametrize("censor_kind", ["halt", "action"])
def test_terminal_path_before_later_halt_or_action_remains_supported(
    censor_kind: str,
) -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    observations = list(series.observations)
    observations[0] = _replace_observation_prices(
        observations[0],
        open_price="104",
        high_price="104.4",
        low_price="103.8",
        close_price="104.1",
    )
    observations[1] = _replace_observation_prices(
        observations[1],
        open_price="104.2",
        high_price="105.3",
        low_price="104",
        close_price="105.2",
    )
    source = observations[0].bar.source_metadata
    cutoff = observations[2].interval_start_at
    statuses = ()
    actions = ()
    coverage = series.coverage_receipt
    if censor_kind == "halt":
        statuses = (
            MarketStatusInterval(
                symbol=series.symbol,
                exchange_session_id=series.exchange_session_id,
                status="".join(("halt", "ed")),
                start=cutoff,
                end=observations[2].interval_end_at,
                reason="later_fixture_halt",
                source_metadata=source,
            ),
        )
        coverage = replace(coverage, status=IntradayCoverageStatus.KNOWN_HALT_GAPS)
    else:
        actions = (
            CorporateActionRecord(
                symbol=series.symbol,
                mapped_symbol=series.symbol,
                action_type="split_announcement",
                effective_at=cutoff,
                exchange_session_id=series.exchange_session_id,
                price_adjustment_basis=PriceAdjustmentBasis.UNADJUSTED,
                source_metadata=source,
                details={"ratio": "2_for_1"},
            ),
        )
    rebuilt = _rebuild_series(
        series,
        coverage=coverage,
        statuses=statuses,
        actions=actions,
        observations=tuple(observations),
    )
    labeled = _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))
    record = next(
        item
        for item in labeled.outcomes
        if item.symbol == "ABC" and item.direction is StrategyDirection.LONG
    )
    assert record.completeness is OutcomeCompleteness.PARTIAL
    assert record.path_status is OutcomePathStatus.TARGET_FIRST
    assert next(
        item for item in record.metrics if item.metric is OutcomeMetric.SIMULATED_GROSS_R
    ).value == Decimal("2")


def test_horizon_local_completeness_ignores_only_later_missing_interval() -> None:
    batch = _batch(missing_symbol="ABC", missing_index=2)
    decision_utc = batch.pipeline_result.decision_at.astimezone(UTC)
    early = build_outcome_horizon(
        decision_at=batch.pipeline_result.decision_at,
        exchange_session_id="XNYS-2026-08-11",
        session_open_at=batch.horizons[0].session_open_at,
        session_close_at=batch.horizons[0].session_close_at,
        kind=OutcomeHorizonKind.ELAPSED_SECONDS,
        elapsed_seconds=121,
    )
    assert early.end_at == decision_utc + timedelta(seconds=121)
    labeled = label_pipeline_outcomes(
        pipeline_result=batch.pipeline_result,
        persistence_receipt=batch.persistence_receipt,
        source_dataset=batch.source_dataset,
        policy=batch.policy,
        horizons=(early, batch.horizons[0]),
        recorded_at=batch.recorded_at,
    )
    abc = [item for item in labeled.outcomes if item.symbol == "ABC"]
    assert [item.completeness for item in abc] == [
        OutcomeCompleteness.COMPLETE,
        OutcomeCompleteness.CENSORED,
        OutcomeCompleteness.COMPLETE,
        OutcomeCompleteness.CENSORED,
    ]
    assert len(labeled.outcomes) == 8
    assert OutcomeLabelBatch.from_json(labeled.to_json()) == labeled


def test_nonempty_pipeline_cannot_silently_declare_no_horizons() -> None:
    batch = _batch()
    with pytest.raises(ValueError, match="at least one outcome horizon"):
        label_pipeline_outcomes(
            pipeline_result=batch.pipeline_result,
            persistence_receipt=batch.persistence_receipt,
            source_dataset=batch.source_dataset,
            policy=batch.policy,
            horizons=(),
            recorded_at=batch.recorded_at,
        )


def test_open_beyond_unproven_entry_is_gap_even_when_bar_ranges_back() -> None:
    evaluation = _empirical_evaluation()
    decision = _gate_one(evaluation, _execution_risk(evaluation=evaluation))
    observations = _path_observations(
        decision,
        (("101", "101.5", "99.5", "100.2"), ("100", "106", "99", "105")),
    )
    assert _resolve_trade_path(decision, observations) == (
        OutcomeEntryStatus.GAP_THROUGH_AMBIGUOUS,
        OutcomePathStatus.GAP_THROUGH_AMBIGUOUS,
    )


def test_standalone_record_rejects_status_risk_and_geometry_rehash_attacks() -> None:
    complete = _batch().outcomes[0]
    status_payload = complete.to_dict()
    status_payload["completeness"] = OutcomeCompleteness.PENDING.value
    with pytest.raises(ValueError, match="entry status, and path status"):
        OutcomeRecord.from_dict(_rehash_record_payload(status_payload))

    partial_batch = _pending_terminal_batch()
    filled = next(
        item for item in partial_batch.outcomes if item.direction is StrategyDirection.LONG
    )
    entry_payload = filled.to_dict()
    entry_payload["modeled_entry_price"] = "103"
    with pytest.raises(ValueError, match="must match StrategyEvaluation"):
        OutcomeRecord.from_dict(_rehash_record_payload(entry_payload))
    exit_payload = filled.to_dict()
    exit_payload["modeled_exit_price"] = "105.1"
    with pytest.raises(ValueError, match="target-first path"):
        OutcomeRecord.from_dict(_rehash_record_payload(exit_payload))
    dropped_risk = filled.to_dict()
    dropped_risk["risk_evidence_id"] = None
    dropped_risk["risk_evidence_content_hash_sha256"] = None
    dropped_risk["risk_evidence"] = None
    with pytest.raises(ValueError, match="embedded decision"):
        OutcomeRecord.from_dict(_rehash_record_payload(dropped_risk))
    noneligible = next(
        item for item in partial_batch.outcomes if item.direction is StrategyDirection.BOTH
    )
    injected = noneligible.to_dict()
    injected["risk_evidence_id"] = filled.risk_evidence_id
    injected["risk_evidence_content_hash_sha256"] = filled.risk_evidence_content_hash_sha256
    injected["risk_evidence"] = filled.risk_evidence.to_dict()
    with pytest.raises(ValueError, match="embedded decision"):
        OutcomeRecord.from_dict(_rehash_record_payload(injected))


def test_standalone_record_rejects_lineage_metric_and_touch_rehash_attacks() -> None:
    filled = next(
        item
        for item in _pending_terminal_batch().outcomes
        if item.direction is StrategyDirection.LONG
    )
    reference_payload = filled.to_dict()
    reference_payload["reference_observation_id"] = filled.source_observation_ids[1]
    reference_payload["reference_observation_content_hash_sha256"] = (
        filled.source_observation_content_hashes[1]
    )
    with pytest.raises(ValueError, match="first source observation"):
        OutcomeRecord.from_dict(_rehash_record_payload(reference_payload))

    lineage_payload = filled.to_dict()
    gross_index = list(OutcomeMetric).index(OutcomeMetric.SIMULATED_GROSS_R)
    lineage_payload["metrics"][gross_index]["source_observation_ids"] = [
        filled.source_observation_ids[0]
    ]
    with pytest.raises(ValueError, match="canonical inputs"):
        OutcomeRecord.from_dict(_rehash_record_payload(lineage_payload))

    missing_mfe = filled.to_dict()
    mfe_index = list(OutcomeMetric).index(OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R)
    missing_mfe["metrics"][mfe_index].update(
        {
            "value": None,
            "status": OutcomeValueStatus.UNAVAILABLE.value,
            "observed_at": None,
            "source_observation_ids": [],
            "method": "outcome_metric_unavailable",
            "reason": "forged_missing_mfe",
        }
    )
    with pytest.raises(ValueError, match="requires MFE and MAE"):
        OutcomeRecord.from_dict(_rehash_record_payload(missing_mfe))

    forged_mfe = filled.to_dict()
    forged_mfe["metrics"][mfe_index]["value"] = "1"
    with pytest.raises(ValueError, match="do not bound|must equal"):
        OutcomeRecord.from_dict(_rehash_record_payload(forged_mfe))

    reordered_touch = filled.to_dict()
    reordered_touch["exit_interval"] = filled.entry_interval.to_dict()
    reordered_touch["target_touch_interval"] = filled.entry_interval.to_dict()
    with pytest.raises(ValueError, match="follow the entry bar"):
        OutcomeRecord.from_dict(_rehash_record_payload(reordered_touch))


def test_noncomplete_record_requires_explicit_reason_and_batch_roundtrip_is_exact() -> None:
    filled = next(
        item
        for item in _pending_terminal_batch().outcomes
        if item.direction is StrategyDirection.LONG
    )
    payload = filled.to_dict()
    payload["reasons"] = []
    with pytest.raises(ValueError, match="requires an explicit reason"):
        OutcomeRecord.from_dict(_rehash_record_payload(payload))


def test_empty_pipeline_allows_empty_horizon_product_and_roundtrip() -> None:
    dataset = _pipeline_dataset()
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=_pipeline_universe(dataset, requested_symbols=()),
        registry=StrategyRegistry(()),
    )
    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation={},
        risk_policy=_pipeline_risk_policy(),
    )
    frozen_at = result.decision_at.astimezone(UTC) + timedelta(seconds=1)
    source = build_outcome_observation_dataset(
        decision_at=result.decision_at,
        frozen_at=frozen_at,
        series=(),
    )
    batch = label_pipeline_outcomes(
        pipeline_result=result,
        persistence_receipt=_receipt(result, result.decision_at.astimezone(UTC)),
        source_dataset=source,
        policy=build_outcome_label_policy(
            policy_version="wp003-b-v1",
            expected_bar_interval_seconds=60,
        ),
        horizons=(),
        recorded_at=frozen_at,
    )
    assert batch.outcomes == ()
    assert batch.horizons == ()
    assert OutcomeLabelBatch.from_json(batch.to_json()) == batch


def test_pre_entry_invalidation_cancels_long_and_short_setups() -> None:
    long_evaluation = _empirical_evaluation()
    long_decision = _gate_one(
        long_evaluation,
        _execution_risk(evaluation=long_evaluation),
    )
    long_invalidated = _path_observations(
        long_decision,
        (("99", "99.5", "97.5", "98"), ("100", "101", "99", "100")),
    )
    assert _resolve_trade_path(long_decision, long_invalidated) == (
        OutcomeEntryStatus.UNATTAINABLE,
        OutcomePathStatus.UNATTAINABLE_FILL,
    )
    same_bar = _path_observations(
        long_decision,
        (("99", "100.5", "97.5", "100"), ("100", "101", "99", "100")),
    )
    assert _resolve_trade_path(long_decision, same_bar) == (
        OutcomeEntryStatus.ENTRY_BAR_AMBIGUOUS,
        OutcomePathStatus.ENTRY_BAR_AMBIGUOUS,
    )
    short_evaluation = replace(
        long_evaluation,
        direction=StrategyDirection.SHORT,
        invalidation_price=Decimal("102"),
        target_price=Decimal("94"),
    )
    short_decision = SimpleNamespace(
        evaluation=short_evaluation,
        direction=StrategyDirection.SHORT,
        decision_at=long_decision.decision_at,
        symbol=long_decision.symbol,
    )
    short_invalidated = _path_observations(
        short_decision,
        (("101", "102.5", "100.5", "102"), ("100", "101", "99", "100")),
    )
    assert _resolve_trade_path(short_decision, short_invalidated) == (
        OutcomeEntryStatus.UNATTAINABLE,
        OutcomePathStatus.UNATTAINABLE_FILL,
    )


@pytest.mark.parametrize(
    "direction,exit_kind",
    [
        (StrategyDirection.LONG, "target"),
        (StrategyDirection.LONG, "stop"),
        (StrategyDirection.SHORT, "target"),
        (StrategyDirection.SHORT, "stop"),
    ],
)
def test_post_entry_open_equal_exit_level_is_exact_touch_not_gap(
    direction: StrategyDirection,
    exit_kind: str,
) -> None:
    base_evaluation = _empirical_evaluation()
    evaluation = (
        base_evaluation
        if direction is StrategyDirection.LONG
        else replace(
            base_evaluation,
            direction=StrategyDirection.SHORT,
            invalidation_price=Decimal("102"),
            target_price=Decimal("94"),
        )
    )
    decision = SimpleNamespace(
        evaluation=evaluation,
        direction=direction,
        decision_at=base_evaluation.decision_at,
        symbol=base_evaluation.symbol,
    )
    if direction is StrategyDirection.LONG:
        exit_bar = (
            ("106", "106", "105", "106")
            if exit_kind == "target"
            else ("98", "99", "98", "98")
        )
    else:
        exit_bar = (
            ("94", "95", "94", "94")
            if exit_kind == "target"
            else ("102", "102", "101", "102")
        )
    observations = _path_observations(
        decision,
        (("100", "100.5", "99.5", "100"), exit_bar),
    )
    expected = (
        OutcomePathStatus.TARGET_FIRST
        if exit_kind == "target"
        else OutcomePathStatus.STOP_FIRST
    )
    assert _resolve_trade_path(decision, observations) == (
        OutcomeEntryStatus.FILLED,
        expected,
    )


def test_strict_deserialization_rejects_unknown_duplicate_schema_and_float_payloads() -> None:
    batch = _batch()
    record_payload = batch.outcomes[0].to_dict()
    record_payload["unknown_top_level"] = "injected"
    with pytest.raises(ValueError, match="unknown field"):
        OutcomeRecord.from_dict(record_payload)

    nested = batch.to_dict()
    nested["pipeline_result"]["preparation"]["unknown_nested"] = "injected"
    with pytest.raises(ValueError, match="unknown field"):
        OutcomeLabelBatch.from_dict(nested)

    duplicate_top = batch.to_json().replace(
        '"batch_id":',
        '"batch_id":"duplicate","batch_id":',
        1,
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        OutcomeLabelBatch.from_json(duplicate_top)
    duplicate_nested = batch.to_json().replace(
        '"source_dataset_id":',
        '"source_dataset_id":"duplicate","source_dataset_id":',
        1,
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        OutcomeLabelBatch.from_json(duplicate_nested)

    schema_payload = batch.outcomes[0].to_dict()
    schema_payload["schema_version"] = "v2.opportunity.outcome_record.v999"
    with pytest.raises(ValueError, match="unsupported schema_version"):
        OutcomeRecord.from_dict(_rehash_record_payload(schema_payload))

    metric_float = batch.outcomes[0].to_dict()
    metric_float["metrics"][0]["value"] = 1.25
    with pytest.raises(ValueError, match="exact Decimal"):
        OutcomeRecord.from_dict(metric_float)
    bar_float = batch.to_dict()
    bar_float["source_dataset"]["series"][0]["observations"][0]["bar"][
        "open_price"
    ] = 100.25
    with pytest.raises(ValueError, match="exact Decimal"):
        OutcomeLabelBatch.from_dict(bar_float)


@pytest.mark.parametrize(
    "details",
    [
        {"api_key": "abc"},
        {"retained_path": "/tmp/outcome.json"},
    ],
)
def test_nested_source_details_reject_secrets_and_absolute_paths(details) -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    source = series.observations[0].bar.source_metadata
    action = CorporateActionRecord(
        symbol=series.symbol,
        mapped_symbol=series.symbol,
        action_type="split_announcement",
        effective_at=series.observations[2].interval_start_at,
        exchange_session_id=series.exchange_session_id,
        price_adjustment_basis=PriceAdjustmentBasis.UNADJUSTED,
        source_metadata=source,
        details=details,
    )
    with pytest.raises(ValueError, match="sensitive key|sanitized"):
        _rebuild_series(series, actions=(action,))


def test_coverage_request_body_and_inventory_contradictions_fail_closed() -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    receipt = series.coverage_receipt
    later_end = receipt.request_end + timedelta(minutes=1)
    extended_source = replace(receipt.source_metadata, request_end=later_end)
    extended = replace(
        receipt,
        request_end=later_end,
        source_metadata=extended_source,
    )
    with pytest.raises(ValueError, match="complete coverage bounds"):
        _rebuild_series(series, coverage=extended)
    duplicated_manifest = replace(
        receipt,
        artifact_manifest_ids=(receipt.artifact_manifest_ids[0],) * 2,
    )
    with pytest.raises(ValueError, match="canonical unique"):
        _rebuild_series(series, coverage=duplicated_manifest)

    partial = _batch(missing_symbol="ABC").source_dataset.series[0]
    unrelated_gap = replace(
        partial.coverage_receipt,
        missing_intervals=(
            (
                partial.coverage_receipt.request_start,
                partial.observations[0].interval_end_at,
            ),
        ),
    )
    with pytest.raises(ValueError, match="do not match retained bar body"):
        _rebuild_series(partial, coverage=unrelated_gap)


def test_terminal_excursion_lineage_is_not_observable_before_exit_bar() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    decision = _gate_one(evaluation, risk)
    observations = list(
        _path_observations(
            decision,
            (("100", "101", "99.5", "100.4"), ("101", "110", "99", "106")),
        )
    )
    later_available = observations[1].available_at + timedelta(microseconds=1)
    later_metadata = replace(
        observations[1].bar.source_metadata,
        fetched_at=later_available,
    )
    observations[1] = build_outcome_bar_evidence(
        bar=replace(observations[1].bar, source_metadata=later_metadata),
        interval_start_at=observations[1].interval_start_at,
        interval_end_at=observations[1].interval_end_at,
    )
    observation_tuple = tuple(observations)
    entry_status, path_status = _resolve_trade_path(decision, observation_tuple)
    path = _path_details(decision, observation_tuple, entry_status, path_status)
    metrics = {
        item.metric: item
        for item in _build_metrics(
            decision=decision,
            risk=risk,
            reference=observation_tuple[0],
            close_observation=None,
            observations=observation_tuple,
            completeness=OutcomeCompleteness.PARTIAL,
            entry_status=entry_status,
            path_status=path_status,
            path=path,
        )
    }
    assert metrics[OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R].value == Decimal("3")
    assert metrics[OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R].value == Decimal("0")
    assert metrics[OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R].observed_at == later_available
    assert metrics[OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R].source_observation_ids[-1] == (
        observation_tuple[1].observation_id
    )


def test_no_entry_uses_explicit_reference_price_not_unfilled_planned_entry() -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    changed = tuple(
        _replace_observation_prices(
            observation,
            open_price="103.7",
            high_price="103.9",
            low_price="103.5",
            close_price="103.8",
        )
        for observation in series.observations
    )
    rebuilt = _rebuild_series(series, observations=changed)
    labeled = _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))
    record = next(
        item
        for item in labeled.outcomes
        if item.symbol == "ABC" and item.direction is StrategyDirection.LONG
    )
    assert record.entry_status is OutcomeEntryStatus.NO_ENTRY
    assert record.path_status is OutcomePathStatus.NO_ENTRY
    assert record.modeled_entry_price is None
    assert record.reference_price == Decimal("103.7")
    reference = next(
        item for item in record.metrics if item.metric is OutcomeMetric.REFERENCE_HORIZON_RETURN
    )
    assert reference.value == (Decimal("103.8") - Decimal("103.7")) / Decimal("103.7")


@pytest.mark.parametrize(
    "direction,exit_kind,exit_bar",
    [
        (StrategyDirection.LONG, "target", ("106", "106", "97", "100")),
        (StrategyDirection.LONG, "stop", ("98", "107", "98", "100")),
        (StrategyDirection.SHORT, "target", ("94", "103", "94", "100")),
        (StrategyDirection.SHORT, "stop", ("102", "102", "93", "100")),
    ],
)
def test_exact_open_exit_resolves_before_same_bar_other_level(
    direction: StrategyDirection,
    exit_kind: str,
    exit_bar: tuple[str, str, str, str],
) -> None:
    base = _empirical_evaluation()
    evaluation = (
        base
        if direction is StrategyDirection.LONG
        else replace(
            base,
            direction=StrategyDirection.SHORT,
            invalidation_price=Decimal("102"),
            target_price=Decimal("94"),
        )
    )
    decision = SimpleNamespace(
        evaluation=evaluation,
        direction=direction,
        decision_at=base.decision_at,
        symbol=base.symbol,
    )
    observations = _path_observations(
        decision,
        (("100", "100.5", "99.5", "100"), exit_bar),
    )
    assert _resolve_trade_path(decision, observations) == (
        OutcomeEntryStatus.FILLED,
        (
            OutcomePathStatus.TARGET_FIRST
            if exit_kind == "target"
            else OutcomePathStatus.STOP_FIRST
        ),
    )


@pytest.mark.parametrize("direction", [StrategyDirection.LONG, StrategyDirection.SHORT])
@pytest.mark.parametrize("path_kind", ["target", "stop", "horizon", "no_entry"])
def test_directional_path_metric_matrix_has_exact_signs_and_formulas(
    direction: StrategyDirection,
    path_kind: str,
) -> None:
    base = _empirical_evaluation()
    evaluation = (
        base
        if direction is StrategyDirection.LONG
        else replace(
            base,
            direction=StrategyDirection.SHORT,
            invalidation_price=Decimal("102"),
            target_price=Decimal("94"),
        )
    )
    risk = _execution_risk(
        evaluation=evaluation,
        base_metrics=_base_risk_metrics(
            entry=Decimal("100"),
            stop=Decimal("98") if direction is StrategyDirection.LONG else Decimal("102"),
            target=Decimal("106") if direction is StrategyDirection.LONG else Decimal("94"),
        ),
    )
    decision = _gate_one(evaluation, risk)
    entry_bar = ("100", "100.5", "99.5", "100")
    if direction is StrategyDirection.LONG:
        exit_bar = {
            "target": ("101", "106", "99", "106"),
            "stop": ("99", "101", "98", "98"),
            "horizon": ("101", "103", "99", "102"),
            "no_entry": ("99", "99.5", "98.5", "99"),
        }[path_kind]
    else:
        exit_bar = {
            "target": ("99", "101", "94", "94"),
            "stop": ("101", "102", "99", "102"),
            "horizon": ("99", "101", "97", "98"),
            "no_entry": ("101", "101.5", "100.5", "101"),
        }[path_kind]
    bars = (exit_bar, exit_bar) if path_kind == "no_entry" else (entry_bar, exit_bar)
    observations = _path_observations(decision, bars)
    entry_status, path_status = _resolve_trade_path(decision, observations)
    if path_kind == "no_entry":
        assert (entry_status, path_status) == (
            OutcomeEntryStatus.NO_ENTRY,
            OutcomePathStatus.NO_ENTRY,
        )
        return
    path = _path_details(decision, observations, entry_status, path_status)
    metrics = {
        item.metric: item
        for item in _build_metrics(
            decision=decision,
            risk=risk,
            reference=observations[0],
            close_observation=observations[-1],
            observations=observations,
            completeness=OutcomeCompleteness.COMPLETE,
            entry_status=entry_status,
            path_status=path_status,
            path=path,
        )
    }
    expected_gross = {
        "target": Decimal("3"),
        "stop": Decimal("-1"),
        "horizon": Decimal("1"),
    }[path_kind]
    expected_after_cost = {
        "target": Decimal("578") / Decimal("222"),
        "stop": Decimal("-1"),
        "horizon": Decimal("178") / Decimal("222"),
    }[path_kind]
    expected_mfe = {
        "target": Decimal("3"),
        "stop": Decimal("0"),
        "horizon": Decimal("1.5"),
    }[path_kind]
    expected_mae = {
        "target": Decimal("0"),
        "stop": Decimal("-1"),
        "horizon": Decimal("-0.5"),
    }[path_kind]
    assert metrics[OutcomeMetric.SIMULATED_GROSS_R].value == expected_gross
    assert metrics[OutcomeMetric.SIMULATED_AFTER_COST_R].value == expected_after_cost
    assert metrics[OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R].value == expected_mfe
    assert metrics[OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R].value == expected_mae


@pytest.mark.parametrize("ambiguity", ["same_bar", "gap_through"])
def test_post_entry_ambiguity_matrix_keeps_trade_metrics_unavailable(ambiguity: str) -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    decision = _gate_one(evaluation, risk)
    exit_bar = (
        ("100", "106", "98", "100")
        if ambiguity == "same_bar"
        else ("107", "108", "106.5", "107")
    )
    observations = _path_observations(
        decision,
        (("100", "101", "99.5", "100"), exit_bar),
    )
    entry_status, path_status = _resolve_trade_path(decision, observations)
    assert entry_status is OutcomeEntryStatus.FILLED
    assert path_status is (
        OutcomePathStatus.SAME_BAR_AMBIGUOUS
        if ambiguity == "same_bar"
        else OutcomePathStatus.GAP_THROUGH_AMBIGUOUS
    )
    path = _path_details(decision, observations, entry_status, path_status)
    metrics = _build_metrics(
        decision=decision,
        risk=risk,
        reference=observations[0],
        close_observation=observations[-1],
        observations=observations,
        completeness=OutcomeCompleteness.CENSORED,
        entry_status=entry_status,
        path_status=path_status,
        path=path,
    )
    assert all(
        item.value is None
        for item in metrics
        if item.metric
        in {
            OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R,
            OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R,
            OutcomeMetric.SIMULATED_GROSS_R,
            OutcomeMetric.SIMULATED_AFTER_COST_R,
        }
    )


def test_entry_bar_ambiguity_has_reference_but_no_trade_metrics() -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    ambiguous = _replace_observation_prices(
        series.observations[0],
        open_price="104",
        high_price="105.3",
        low_price="103.8",
        close_price="104.5",
    )
    labeled = _relabel_with_series(
        batch,
        (
            _rebuild_series(series, observations=(ambiguous, *series.observations[1:])),
            batch.source_dataset.series[1],
        ),
    )
    record = next(
        item
        for item in labeled.outcomes
        if item.symbol == "ABC" and item.direction is StrategyDirection.LONG
    )
    assert record.path_status is OutcomePathStatus.ENTRY_BAR_AMBIGUOUS
    assert next(
        item for item in record.metrics if item.metric is OutcomeMetric.REFERENCE_HORIZON_RETURN
    ).value is not None
    assert all(
        item.value is None
        for item in record.metrics
        if item.metric is not OutcomeMetric.REFERENCE_HORIZON_RETURN
    )


@pytest.mark.parametrize("unresolved_receipt", [False, True])
def test_corporate_action_before_entry_or_unresolved_status_censors(
    unresolved_receipt: bool,
) -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    source = series.observations[0].bar.source_metadata
    coverage = series.coverage_receipt
    actions = ()
    if unresolved_receipt:
        coverage = replace(
            coverage,
            status=IntradayCoverageStatus.CORPORATE_ACTION_UNRESOLVED,
        )
    else:
        actions = (
            CorporateActionRecord(
                symbol=series.symbol,
                mapped_symbol=series.symbol,
                action_type="split_announcement",
                effective_at=series.observations[0].interval_start_at,
                exchange_session_id=series.exchange_session_id,
                price_adjustment_basis=PriceAdjustmentBasis.UNADJUSTED,
                source_metadata=source,
                details={"ratio": "2_for_1"},
            ),
        )
    rebuilt = _rebuild_series(series, coverage=coverage, actions=actions)
    labeled = _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))
    affected = [item for item in labeled.outcomes if item.symbol == "ABC"]
    assert all(item.completeness is OutcomeCompleteness.CENSORED for item in affected)
    assert all(
        item.path_status is OutcomePathStatus.CORPORATE_ACTION_CENSORED
        for item in affected
    )


def test_bar_interval_start_equal_decision_is_rejected_by_series_boundary() -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    observation = series.observations[0]
    decision_utc = batch.pipeline_result.decision_at.astimezone(UTC)
    metadata = replace(
        observation.bar.source_metadata,
        request_start=decision_utc,
    )
    at_decision = build_outcome_bar_evidence(
        bar=replace(observation.bar, source_metadata=metadata),
        interval_start_at=decision_utc,
        interval_end_at=observation.interval_end_at,
    )
    with pytest.raises(ValueError, match="does not match series scope"):
        _rebuild_series(
            series,
            observations=(at_decision, *series.observations[1:]),
        )


def test_session_close_horizon_equation_and_roundtrip() -> None:
    batch = _batch()
    horizon = build_outcome_horizon(
        decision_at=batch.pipeline_result.decision_at,
        exchange_session_id="XNYS-2026-08-11",
        session_open_at=batch.horizons[0].session_open_at,
        session_close_at=batch.horizons[0].session_close_at,
        kind=OutcomeHorizonKind.SESSION_CLOSE,
    )
    assert horizon.end_at == horizon.session_close_at
    assert horizon.elapsed_seconds is None
    assert type(horizon).from_json(horizon.to_json()) == horizon


def test_three_horizon_batch_has_exact_evaluation_major_twelve_record_product() -> None:
    batch = _batch()
    early = build_outcome_horizon(
        decision_at=batch.pipeline_result.decision_at,
        exchange_session_id="XNYS-2026-08-11",
        session_open_at=batch.horizons[0].session_open_at,
        session_close_at=batch.horizons[0].session_close_at,
        kind=OutcomeHorizonKind.ELAPSED_SECONDS,
        elapsed_seconds=121,
    )
    session_close = build_outcome_horizon(
        decision_at=batch.pipeline_result.decision_at,
        exchange_session_id="XNYS-2026-08-11",
        session_open_at=batch.horizons[0].session_open_at,
        session_close_at=batch.horizons[0].session_close_at,
        kind=OutcomeHorizonKind.SESSION_CLOSE,
    )
    horizons = (early, batch.horizons[0], session_close)
    labeled = label_pipeline_outcomes(
        pipeline_result=batch.pipeline_result,
        persistence_receipt=batch.persistence_receipt,
        source_dataset=batch.source_dataset,
        policy=batch.policy,
        horizons=horizons,
        recorded_at=batch.recorded_at,
    )
    assert len(labeled.outcomes) == 12
    assert tuple((item.evaluation_id, item.horizon_id) for item in labeled.outcomes) == tuple(
        (evaluation.evaluation_id, horizon.horizon_id)
        for evaluation in batch.pipeline_result.evaluations
        for horizon in horizons
    )


def test_future_bar_mutation_changes_only_downstream_source_and_label_identities() -> None:
    batch = _batch()
    series = batch.source_dataset.series[0]
    final = series.observations[-1]
    mutated_final = _replace_observation_prices(
        final,
        open_price=format(final.bar.open_price, "f"),
        high_price=format(final.bar.high_price, "f"),
        low_price=format(final.bar.low_price, "f"),
        close_price=format(final.bar.close_price + Decimal("0.01"), "f"),
    )
    rebuilt = _rebuild_series(
        series,
        observations=(*series.observations[:-1], mutated_final),
    )
    changed = _relabel_with_series(batch, (rebuilt, batch.source_dataset.series[1]))
    assert changed.pipeline_result.run_id == batch.pipeline_result.run_id
    assert changed.pipeline_result.preparation.preparation_id == (
        batch.pipeline_result.preparation.preparation_id
    )
    assert tuple(item.evaluation_id for item in changed.pipeline_result.evaluations) == tuple(
        item.evaluation_id for item in batch.pipeline_result.evaluations
    )
    assert tuple(item.decision_id for item in changed.pipeline_result.decisions) == tuple(
        item.decision_id for item in batch.pipeline_result.decisions
    )
    assert tuple(item.trace_id for item in changed.pipeline_result.traces) == tuple(
        item.trace_id for item in batch.pipeline_result.traces
    )
    assert changed.source_dataset.source_dataset_id != batch.source_dataset.source_dataset_id
    assert changed.batch_id != batch.batch_id
    changed_abc = tuple(item.outcome_id for item in changed.outcomes if item.symbol == "ABC")
    original_abc = tuple(item.outcome_id for item in batch.outcomes if item.symbol == "ABC")
    assert changed_abc != original_abc


def test_explicit_take_outcome_contract_remains_nonpromotable() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    take = _gate_one(evaluation, risk)
    assert take.decision is TradeDecisionValue.TAKE
    template = next(
        item
        for item in _pending_terminal_batch().outcomes
        if item.direction is StrategyDirection.LONG
    )
    payload = template.to_dict()
    take_horizon = build_outcome_horizon(
        decision_at=take.decision_at,
        exchange_session_id="XNYS-2026-08-11",
        session_open_at=datetime(2026, 8, 11, 14, 30, tzinfo=UTC),
        session_close_at=datetime(2026, 8, 11, 21, 0, tzinfo=UTC),
        kind=OutcomeHorizonKind.ELAPSED_SECONDS,
        elapsed_seconds=301,
    )
    observations = _path_observations(
        take,
        (
            ("100", "101", "99.5", "100.4"),
            ("101", "106.2", "100.5", "106"),
        ),
    )
    source = observations[0].bar.source_metadata
    coverage = IntradayCoverageReceipt(
        coverage_receipt_id="coverage-explicit-take",
        provider=source.provider,
        feed=source.feed,
        entitlement=source.entitlement,
        symbol=take.symbol,
        market_date="2026-08-11",
        exchange_session_id=source.exchange_session_id,
        request_start=source.request_start,
        request_end=source.request_end,
        status=IntradayCoverageStatus.COMPLETE,
        source_metadata=source,
        observed_start=observations[0].interval_start_at,
        observed_end=observations[-1].interval_end_at,
        reason="explicit_take_fixture_coverage",
        created_at=source.fetched_at,
    )
    series = build_outcome_observation_series(
        symbol=take.symbol,
        exchange_session_id=source.exchange_session_id,
        decision_at=take.decision_at,
        requested_through_at=observations[-1].interval_end_at,
        coverage_receipt=coverage,
        observations=observations,
        source_identity="explicit_take_fixture_source",
        method="retained_post_decision_take_fixture",
    )
    entry_status, path_status = _resolve_trade_path(take, observations)
    path = _path_details(take, observations, entry_status, path_status)
    metrics = _build_metrics(
        decision=take,
        risk=risk,
        reference=observations[0],
        close_observation=None,
        observations=observations,
        completeness=OutcomeCompleteness.PARTIAL,
        entry_status=entry_status,
        path_status=path_status,
        path=path,
    )
    payload.update(
        {
            "evaluation_id": take.evaluation_id,
            "evaluation_content_hash_sha256": take.evaluation.content_hash(),
            "decision_id": take.decision_id,
            "decision_content_hash_sha256": take.content_hash(),
            "decision": take.to_dict(),
            "risk_evidence_id": risk.execution_risk_evidence_id,
            "risk_evidence_content_hash_sha256": risk.content_hash(),
            "risk_evidence": risk.to_dict(),
            "decision_at": take.decision_at.isoformat(),
            "symbol": take.symbol,
            "strategy_id": take.strategy_id,
            "strategy_version": take.strategy_version,
            "direction": take.direction.value,
            "decision_value": take.decision.value,
            "horizon_id": take_horizon.horizon_id,
            "horizon_content_hash_sha256": take_horizon.content_hash(),
            "horizon": take_horizon.to_dict(),
            "source_series_id": series.series_id,
            "source_series_content_hash_sha256": series.content_hash(),
            "source_series": series.to_dict(),
            "source_frozen_at": source.fetched_at.isoformat(),
            "recorded_at": source.fetched_at.isoformat(),
            "completeness": OutcomeCompleteness.PARTIAL.value,
            "entry_status": entry_status.value,
            "path_status": path_status.value,
            "reference_price_kind": OutcomeReferencePriceKind.FIRST_POST_DECISION_OPEN.value,
            "reference_price": str(observations[0].bar.open_price),
            "reference_observation_id": observations[0].observation_id,
            "reference_observation_content_hash_sha256": observations[0].content_hash(),
            "horizon_close_price": None,
            "horizon_close_observation_id": None,
            "horizon_close_observation_content_hash_sha256": None,
            "modeled_entry_price": str(path.entry_price),
            "modeled_exit_price": str(path.exit_price),
            "entry_interval": _touch_interval(path.entry_observation).to_dict(),
            "exit_interval": _touch_interval(path.exit_observation).to_dict(),
            "target_touch_interval": _touch_interval(path.target_observation).to_dict(),
            "stop_touch_interval": None,
            "source_observations": [item.to_dict() for item in observations],
            "source_observation_ids": [item.observation_id for item in observations],
            "source_observation_content_hashes": [
                item.content_hash() for item in observations
            ],
            "metrics": [item.to_dict() for item in metrics],
            "reasons": ["terminal_path_resolved_before_horizon_end"],
            "limitations": ["retrospective_bar_interval_resolution"],
            "promotion_eligible": False,
        }
    )
    outcome = OutcomeRecord.from_dict(_rehash_record_payload(payload))
    assert outcome.decision_value is TradeDecisionValue.TAKE
    assert outcome.promotion_eligible is False
    assert outcome.retrospective_research_only is True
