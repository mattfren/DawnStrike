"""Pure causal outcome batch reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from intraday_scanner.storage.opportunity_store import OpportunityPersistenceReceipt
from intraday_scanner.v2.opportunity.models import (
    OpportunityContract,
    TradeDecision,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    OutcomeHorizon,
    OutcomeLabelPolicy,
    OutcomeReferencePriceKind,
    _identity_payload,
    _require_identity,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
    _require_utc,
)
from intraday_scanner.v2.opportunity.outcome_records import OutcomeRecord
from intraday_scanner.v2.opportunity.outcome_resolution import (
    _build_metrics,
    _path_details,
    _resolve_record_state,
    _touch_interval,
)
from intraday_scanner.v2.opportunity.outcome_sources import (
    OutcomeObservationDataset,
    OutcomeObservationSeries,
)
from intraday_scanner.v2.opportunity.pipeline import PipelineResult
from intraday_scanner.v2.opportunity.risk import ExecutionRiskEvidence


@dataclass(frozen=True)
class OutcomeLabelBatch(OutcomeContract):
    batch_id: str
    pipeline_result: PipelineResult
    persistence_receipt: OpportunityPersistenceReceipt
    source_dataset: OutcomeObservationDataset
    policy: OutcomeLabelPolicy
    horizons: tuple[OutcomeHorizon, ...]
    recorded_at: datetime
    outcomes: tuple[OutcomeRecord, ...]
    limitations: tuple[str, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.outcome_label_batch.v2"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.outcome_label_batch.v2")
        _require_identity(self.batch_id, "batch_id")
        _require_utc(self.recorded_at, "recorded_at")
        _validate_receipt_result_binding(self.pipeline_result, self.persistence_receipt)
        if (
            self.source_dataset.decision_at != self.pipeline_result.decision_at
            or self.recorded_at < self.source_dataset.frozen_at
            or self.recorded_at < self.persistence_receipt.recorded_at
        ):
            raise ValueError("outcome source dataset does not match batch chronology")
        if self.pipeline_result.evaluations and not self.horizons:
            raise ValueError("nonempty evaluations require at least one outcome horizon")
        horizon_order = tuple(
            sorted(self.horizons, key=lambda item: (item.end_at, item.horizon_id))
        )
        horizon_ids = tuple(item.horizon_id for item in self.horizons)
        if self.horizons != horizon_order or len(horizon_ids) != len(set(horizon_ids)):
            raise ValueError("outcome horizons must use canonical unique order")
        if any(item.decision_at != self.pipeline_result.decision_at for item in self.horizons):
            raise ValueError("outcome horizon decision_at does not match pipeline")
        expected_symbols = tuple(sorted({item.symbol for item in self.pipeline_result.evaluations}))
        if tuple(item.symbol for item in self.source_dataset.series) != expected_symbols:
            raise ValueError("outcome source series must exactly match evaluation symbols")
        expected_pairs = tuple(
            (evaluation.evaluation_id, horizon.horizon_id)
            for evaluation in self.pipeline_result.evaluations
            for horizon in self.horizons
        )
        actual_pairs = tuple((item.evaluation_id, item.horizon_id) for item in self.outcomes)
        if actual_pairs != expected_pairs:
            raise ValueError("outcomes must exactly reconcile evaluation-horizon Cartesian order")
        for record in self.outcomes:
            _validate_batch_record_binding(self, record)
        expected_outcomes = _resolve_outcomes(
            result=self.pipeline_result,
            receipt=self.persistence_receipt,
            source_dataset=self.source_dataset,
            policy=self.policy,
            horizons=self.horizons,
            recorded_at=self.recorded_at,
        )
        if self.outcomes != expected_outcomes:
            raise ValueError("outcome records do not match deterministic source resolution")
        if not self.research_only:
            raise ValueError("outcome label batch must remain research_only")
        _require_unique(list(self.limitations), "outcome label batch limitation")
        for limitation in self.limitations:
            _require_sanitized_text(limitation, "outcome label batch limitation")
        expected = stable_identity("outcome-label-batch", _identity_payload(self, "batch_id"))
        if self.batch_id != expected:
            raise ValueError("outcome label batch identity does not match content")


def label_pipeline_outcomes(
    *,
    pipeline_result: PipelineResult,
    persistence_receipt: OpportunityPersistenceReceipt,
    source_dataset: OutcomeObservationDataset,
    policy: OutcomeLabelPolicy,
    horizons: tuple[OutcomeHorizon, ...],
    recorded_at: datetime,
    limitations: tuple[str, ...] = ("bounded_replay_not_promotion_eligible",),
) -> OutcomeLabelBatch:
    """Resolve one immutable retrospective label for every evaluation and horizon."""

    outcomes = _resolve_outcomes(
        result=pipeline_result,
        receipt=persistence_receipt,
        source_dataset=source_dataset,
        policy=policy,
        horizons=horizons,
        recorded_at=recorded_at,
    )
    values = {
        "pipeline_result": pipeline_result,
        "persistence_receipt": persistence_receipt,
        "source_dataset": source_dataset,
        "policy": policy,
        "horizons": horizons,
        "recorded_at": recorded_at,
        "outcomes": outcomes,
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.outcome_label_batch.v2",
    }
    return OutcomeLabelBatch(
        batch_id=stable_identity("outcome-label-batch", values),
        pipeline_result=pipeline_result,
        persistence_receipt=persistence_receipt,
        source_dataset=source_dataset,
        policy=policy,
        horizons=horizons,
        recorded_at=recorded_at,
        outcomes=outcomes,
        limitations=limitations,
    )


def _resolve_outcomes(
    *,
    result: PipelineResult,
    receipt: OpportunityPersistenceReceipt,
    source_dataset: OutcomeObservationDataset,
    policy: OutcomeLabelPolicy,
    horizons: tuple[OutcomeHorizon, ...],
    recorded_at: datetime,
) -> tuple[OutcomeRecord, ...]:
    _require_utc(recorded_at, "recorded_at")
    _validate_receipt_result_binding(result, receipt)
    if recorded_at < receipt.recorded_at:
        raise ValueError("outcome batch cannot precede persistence receipt")
    if source_dataset.decision_at != result.decision_at:
        raise ValueError("outcome source dataset decision_at does not match pipeline")
    if result.evaluations and not horizons:
        raise ValueError("nonempty evaluations require at least one outcome horizon")
    ordered_horizons = tuple(sorted(horizons, key=lambda item: (item.end_at, item.horizon_id)))
    if horizons != ordered_horizons:
        raise ValueError("outcome horizons must use canonical order")
    decision_map = {item.evaluation_id: item for item in result.decisions}
    risk_map = {item.evaluation_id: item for item in result.risk_evidence}
    series_map = {item.symbol: item for item in source_dataset.series}
    if set(series_map) != {item.symbol for item in result.evaluations}:
        raise ValueError("outcome source series must exactly match evaluation symbols")
    if any(
        horizon.exchange_session_id != series_map[evaluation.symbol].exchange_session_id
        for evaluation in result.evaluations
        for horizon in horizons
    ):
        raise ValueError("outcome horizon session does not match symbol source series")
    return tuple(
        _resolve_record(
            result=result,
            receipt=receipt,
            source_dataset=source_dataset,
            policy=policy,
            horizon=horizon,
            decision=decision_map[evaluation.evaluation_id],
            risk=risk_map.get(evaluation.evaluation_id),
            series=series_map[evaluation.symbol],
            recorded_at=recorded_at,
        )
        for evaluation in result.evaluations
        for horizon in horizons
    )


def _resolve_record(
    *,
    result: PipelineResult,
    receipt: OpportunityPersistenceReceipt,
    source_dataset: OutcomeObservationDataset,
    policy: OutcomeLabelPolicy,
    horizon: OutcomeHorizon,
    decision: TradeDecision,
    risk: ExecutionRiskEvidence | None,
    series: OutcomeObservationSeries,
    recorded_at: datetime,
) -> OutcomeRecord:
    state = _resolve_record_state(
        decision=decision,
        series=series,
        policy=policy,
        horizon=horizon,
        source_frozen_at=source_dataset.frozen_at,
        recorded_at=recorded_at,
    )
    observations = state.observations
    reference = state.reference
    resolution_observations = state.resolution_observations
    completeness = state.completeness
    entry_status = state.entry_status
    path_status = state.path_status
    reasons = list(state.reasons)
    close_observation = state.close_observation
    path = _path_details(
        decision,
        resolution_observations,
        entry_status,
        path_status,
    )
    source_ids = tuple(item.observation_id for item in observations)
    source_hashes = tuple(item.content_hash() for item in observations)
    metrics = _build_metrics(
        decision=decision,
        risk=risk,
        reference=reference,
        close_observation=close_observation,
        observations=resolution_observations,
        completeness=completeness,
        entry_status=entry_status,
        path_status=path_status,
        path=path,
    )
    entry_interval = _touch_interval(path.entry_observation)
    exit_interval = _touch_interval(path.exit_observation)
    target_interval = _touch_interval(path.target_observation)
    stop_interval = _touch_interval(path.stop_observation)
    values = {
        "persistence_receipt_id": receipt.receipt_id,
        "persistence_receipt_content_hash_sha256": receipt.content_hash(),
        "pipeline_run_id": result.run_id,
        "pipeline_run_content_hash_sha256": result.content_hash(),
        "preparation_id": result.preparation.preparation_id,
        "preparation_content_hash_sha256": result.preparation.content_hash(),
        "evaluation_id": decision.evaluation_id,
        "evaluation_content_hash_sha256": decision.evaluation.content_hash(),
        "decision_id": decision.decision_id,
        "decision_content_hash_sha256": decision.content_hash(),
        "decision": decision,
        "risk_evidence_id": risk.execution_risk_evidence_id if risk else None,
        "risk_evidence_content_hash_sha256": risk.content_hash() if risk else None,
        "risk_evidence": risk,
        "decision_at": decision.decision_at,
        "symbol": decision.symbol,
        "strategy_id": decision.strategy_id,
        "strategy_version": decision.strategy_version,
        "direction": decision.direction,
        "decision_value": decision.decision,
        "horizon_id": horizon.horizon_id,
        "horizon_content_hash_sha256": horizon.content_hash(),
        "horizon": horizon,
        "policy_id": policy.policy_id,
        "policy_content_hash_sha256": policy.content_hash(),
        "policy": policy,
        "source_dataset_id": source_dataset.source_dataset_id,
        "source_dataset_content_hash_sha256": source_dataset.content_hash(),
        "source_series_id": series.series_id,
        "source_series_content_hash_sha256": series.content_hash(),
        "source_series": series,
        "source_frozen_at": source_dataset.frozen_at,
        "recorded_at": recorded_at,
        "completeness": completeness,
        "entry_status": entry_status,
        "path_status": path_status,
        "reference_price_kind": (
            OutcomeReferencePriceKind.FIRST_POST_DECISION_OPEN
            if reference is not None
            else OutcomeReferencePriceKind.UNAVAILABLE
        ),
        "reference_price": reference.bar.open_price if reference else None,
        "reference_observation_id": reference.observation_id if reference else None,
        "reference_observation_content_hash_sha256": (
            reference.content_hash() if reference else None
        ),
        "horizon_close_price": close_observation.bar.close_price if close_observation else None,
        "horizon_close_observation_id": (
            close_observation.observation_id if close_observation else None
        ),
        "horizon_close_observation_content_hash_sha256": (
            close_observation.content_hash() if close_observation else None
        ),
        "modeled_entry_price": path.entry_price,
        "modeled_exit_price": path.exit_price,
        "entry_interval": entry_interval,
        "exit_interval": exit_interval,
        "target_touch_interval": target_interval,
        "stop_touch_interval": stop_interval,
        "source_observations": observations,
        "source_observation_ids": source_ids,
        "source_observation_content_hashes": source_hashes,
        "metrics": metrics,
        "reasons": tuple(reasons),
        "limitations": tuple(
            dict.fromkeys((*series.limitations, "retrospective_bar_interval_resolution"))
        ),
        "retrospective_research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.outcome_record.v3",
    }
    return OutcomeRecord(
        outcome_id=stable_identity("opportunity-outcome", values),
        **values,  # type: ignore[arg-type]
    )


def _validate_receipt_result_binding(
    result: PipelineResult,
    receipt: OpportunityPersistenceReceipt,
) -> None:
    _require_schema(result.schema_version, "v2.opportunity.pipeline_result.v2")
    supported_receipt_pairs = {
        ("v2.opportunity.persistence_receipt.v1", 27),
        ("v2.opportunity.persistence_receipt.v2", 28),
    }
    if (receipt.schema_version, receipt.database_schema_version) not in (
        supported_receipt_pairs
    ):
        raise ValueError("unsupported opportunity persistence receipt schema/database pair")
    context = result.decision_context
    if (
        receipt.run_id != result.run_id
        or receipt.run_content_hash_sha256 != result.content_hash()
        or receipt.preparation_id != result.preparation.preparation_id
        or receipt.preparation_content_hash_sha256 != result.preparation.content_hash()
        or receipt.decision_at != result.decision_at
        or receipt.research_only is not result.research_only
        or receipt.decision_context_id
        != (context.decision_run_id if context is not None else None)
        or receipt.decision_context_content_hash_sha256
        != (context.content_hash() if context is not None else None)
    ):
        raise ValueError("opportunity persistence receipt does not match pipeline result")


def _validate_batch_record_binding(batch: OutcomeLabelBatch, record: OutcomeRecord) -> None:
    result = batch.pipeline_result
    decision = next(item for item in result.decisions if item.evaluation_id == record.evaluation_id)
    risk = next(
        (item for item in result.risk_evidence if item.evaluation_id == record.evaluation_id),
        None,
    )
    horizon = next(item for item in batch.horizons if item.horizon_id == record.horizon_id)
    series = next(item for item in batch.source_dataset.series if item.symbol == record.symbol)
    expected = (
        batch.persistence_receipt.receipt_id,
        batch.persistence_receipt.content_hash(),
        result.run_id,
        result.content_hash(),
        result.preparation.preparation_id,
        result.preparation.content_hash(),
        decision,
        decision.content_hash(),
        risk,
        risk.content_hash() if risk else None,
        horizon,
        horizon.content_hash(),
        batch.policy,
        batch.policy.content_hash(),
        batch.source_dataset.source_dataset_id,
        batch.source_dataset.content_hash(),
        series.series_id,
        series.content_hash(),
        series,
        batch.source_dataset.frozen_at,
        batch.recorded_at,
    )
    actual = (
        record.persistence_receipt_id,
        record.persistence_receipt_content_hash_sha256,
        record.pipeline_run_id,
        record.pipeline_run_content_hash_sha256,
        record.preparation_id,
        record.preparation_content_hash_sha256,
        record.decision,
        record.decision_content_hash_sha256,
        record.risk_evidence,
        record.risk_evidence_content_hash_sha256,
        record.horizon,
        record.horizon_content_hash_sha256,
        record.policy,
        record.policy_content_hash_sha256,
        record.source_dataset_id,
        record.source_dataset_content_hash_sha256,
        record.source_series_id,
        record.source_series_content_hash_sha256,
        record.source_series,
        record.source_frozen_at,
        record.recorded_at,
    )
    if actual != expected:
        raise ValueError("outcome record does not exactly bind batch inputs")
    local = tuple(
        item for item in series.observations if item.interval_end_at <= horizon.end_at
    )
    if record.source_observation_ids != tuple(item.observation_id for item in local) or (
        record.source_observation_content_hashes != tuple(item.content_hash() for item in local)
    ):
        raise ValueError("outcome record source lineage does not match horizon-local series")
    local_map = {item.observation_id: item for item in local}
    referenced_ids = set(record.source_observation_ids)
    for interval in (
        record.entry_interval,
        record.exit_interval,
        record.target_touch_interval,
        record.stop_touch_interval,
    ):
        if interval is not None:
            observation = local_map.get(interval.observation_id)
            if observation is None or (
                interval.observation_content_hash_sha256 != observation.content_hash()
            ):
                raise ValueError("outcome touch lineage is absent from horizon-local series")
    for metric in record.metrics:
        if not set(metric.source_observation_ids).issubset(referenced_ids):
            raise ValueError("outcome metric lineage is absent from horizon-local series")
        if metric.observed_at is not None and metric.observed_at > batch.recorded_at:
            raise ValueError("outcome metric was not observable when batch was recorded")
