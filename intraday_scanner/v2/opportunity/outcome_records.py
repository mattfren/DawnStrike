"""Standalone self-verifying outcome records and direct invariants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from intraday_scanner.v2.opportunity.models import (
    OpportunityContract,
    StrategyDirection,
    TradeDecision,
    TradeDecisionValue,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_contracts import (
    CANONICAL_OUTCOME_METRICS,
    OutcomeCompleteness,
    OutcomeContract,
    OutcomeEntryStatus,
    OutcomeHorizon,
    OutcomeLabelPolicy,
    OutcomeMetric,
    OutcomeNumericEvidence,
    OutcomePathStatus,
    OutcomeReferencePriceKind,
    OutcomeTouchInterval,
    OutcomeValueStatus,
    _after_cost_value,
    _direction_sign,
    _identity_payload,
    _require_aware,
    _require_hash,
    _require_identity,
    _require_positive_decimal,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
    _require_utc,
    _timedelta_decimal_seconds,
)
from intraday_scanner.v2.opportunity.outcome_resolution import (
    _build_metrics,
    _path_details,
    _resolve_record_state,
    _touch_interval,
)
from intraday_scanner.v2.opportunity.outcome_sources import (
    OutcomeBarEvidence,
    OutcomeObservationSeries,
)
from intraday_scanner.v2.opportunity.risk import ExecutionRiskEvidence


@dataclass(frozen=True)
class OutcomeRecord(OutcomeContract):
    outcome_id: str
    persistence_receipt_id: str
    persistence_receipt_content_hash_sha256: str
    pipeline_run_id: str
    pipeline_run_content_hash_sha256: str
    preparation_id: str
    preparation_content_hash_sha256: str
    evaluation_id: str
    evaluation_content_hash_sha256: str
    decision_id: str
    decision_content_hash_sha256: str
    decision: TradeDecision
    risk_evidence_id: str | None
    risk_evidence_content_hash_sha256: str | None
    risk_evidence: ExecutionRiskEvidence | None
    decision_at: datetime
    symbol: str
    strategy_id: str
    strategy_version: str
    direction: StrategyDirection
    decision_value: TradeDecisionValue
    horizon_id: str
    horizon_content_hash_sha256: str
    horizon: OutcomeHorizon
    policy_id: str
    policy_content_hash_sha256: str
    policy: OutcomeLabelPolicy
    source_dataset_id: str
    source_dataset_content_hash_sha256: str
    source_series_id: str
    source_series_content_hash_sha256: str
    source_series: OutcomeObservationSeries
    source_frozen_at: datetime
    recorded_at: datetime
    completeness: OutcomeCompleteness
    entry_status: OutcomeEntryStatus
    path_status: OutcomePathStatus
    reference_price_kind: OutcomeReferencePriceKind
    reference_price: Decimal | None
    reference_observation_id: str | None
    reference_observation_content_hash_sha256: str | None
    horizon_close_price: Decimal | None
    horizon_close_observation_id: str | None
    horizon_close_observation_content_hash_sha256: str | None
    modeled_entry_price: Decimal | None
    modeled_exit_price: Decimal | None
    entry_interval: OutcomeTouchInterval | None
    exit_interval: OutcomeTouchInterval | None
    target_touch_interval: OutcomeTouchInterval | None
    stop_touch_interval: OutcomeTouchInterval | None
    source_observations: tuple[OutcomeBarEvidence, ...]
    source_observation_ids: tuple[str, ...]
    source_observation_content_hashes: tuple[str, ...]
    metrics: tuple[OutcomeNumericEvidence, ...]
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    retrospective_research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.outcome_record.v3"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.outcome_record.v3")
        for value, name in (
            (self.outcome_id, "outcome_id"),
            (self.persistence_receipt_id, "persistence_receipt_id"),
            (self.pipeline_run_id, "pipeline_run_id"),
            (self.preparation_id, "preparation_id"),
            (self.evaluation_id, "evaluation_id"),
            (self.decision_id, "decision_id"),
            (self.symbol, "symbol"),
            (self.strategy_id, "strategy_id"),
            (self.strategy_version, "strategy_version"),
            (self.horizon_id, "horizon_id"),
            (self.policy_id, "policy_id"),
            (self.source_dataset_id, "source_dataset_id"),
            (self.source_series_id, "source_series_id"),
        ):
            _require_identity(value, name)
        for value, name in (
            (self.persistence_receipt_content_hash_sha256, "persistence_receipt hash"),
            (self.pipeline_run_content_hash_sha256, "pipeline_run hash"),
            (self.preparation_content_hash_sha256, "preparation hash"),
            (self.evaluation_content_hash_sha256, "evaluation hash"),
            (self.decision_content_hash_sha256, "decision hash"),
            (self.horizon_content_hash_sha256, "horizon hash"),
            (self.policy_content_hash_sha256, "policy hash"),
            (self.source_dataset_content_hash_sha256, "source dataset hash"),
            (self.source_series_content_hash_sha256, "source series hash"),
        ):
            _require_hash(value, name)
        _require_aware(self.decision_at, "decision_at")
        _require_utc(self.source_frozen_at, "source_frozen_at")
        _require_utc(self.recorded_at, "recorded_at")
        decision_metadata = (
            self.decision.decision_id,
            self.decision.content_hash(),
            self.decision.evaluation_id,
            self.decision.evaluation.content_hash(),
            self.decision.decision_at,
            self.decision.symbol,
            self.decision.strategy_id,
            self.decision.strategy_version,
            self.decision.direction,
            self.decision.decision,
        )
        record_metadata = (
            self.decision_id,
            self.decision_content_hash_sha256,
            self.evaluation_id,
            self.evaluation_content_hash_sha256,
            self.decision_at,
            self.symbol,
            self.strategy_id,
            self.strategy_version,
            self.direction,
            self.decision_value,
        )
        if record_metadata != decision_metadata:
            raise ValueError("outcome record metadata does not match embedded decision")
        if self.horizon_id != self.horizon.horizon_id or (
            self.horizon_content_hash_sha256 != self.horizon.content_hash()
        ):
            raise ValueError("outcome horizon reference does not match embedded horizon")
        if self.policy_id != self.policy.policy_id or (
            self.policy_content_hash_sha256 != self.policy.content_hash()
        ):
            raise ValueError("outcome policy reference does not match embedded policy")
        if self.decision_at != self.horizon.decision_at or self.recorded_at < self.decision_at:
            raise ValueError("outcome record chronology does not match decision/horizon")
        if (
            self.source_series_id != self.source_series.series_id
            or self.source_series_content_hash_sha256 != self.source_series.content_hash()
            or self.source_series.decision_at != self.decision_at
            or self.source_series.symbol != self.symbol
            or self.source_series.exchange_session_id != self.horizon.exchange_session_id
            or self.source_frozen_at <= self.decision_at
            or self.source_frozen_at > self.recorded_at
        ):
            raise ValueError("outcome record source series binding does not match content")
        receipt = self.source_series.coverage_receipt
        source_fact_times = (
            *(item.available_at for item in self.source_series.observations),
            receipt.source_metadata.fetched_at,
            receipt.created_at or receipt.source_metadata.fetched_at,
            *(
                item.source_metadata.fetched_at
                for item in self.source_series.market_status_intervals
            ),
            *(
                item.source_metadata.fetched_at
                for item in self.source_series.corporate_actions
            ),
        )
        if any(item > self.source_frozen_at for item in source_fact_times):
            raise ValueError("outcome source fact was unavailable at source_frozen_at")
        if (self.risk_evidence_id is None) is not (self.risk_evidence is None) or (
            self.risk_evidence_id is None
        ) is not (self.risk_evidence_content_hash_sha256 is None):
            raise ValueError("outcome risk identity, hash, and evidence must be present together")
        if (
            self.risk_evidence_id != self.decision.risk_evidence_id
            or self.risk_evidence_content_hash_sha256
            != self.decision.risk_evidence_content_hash
        ):
            raise ValueError("outcome risk binding does not match embedded decision")
        if self.risk_evidence is not None and (
            self.risk_evidence_id != self.risk_evidence.execution_risk_evidence_id
            or self.risk_evidence_content_hash_sha256 != self.risk_evidence.content_hash()
            or self.risk_evidence.evaluation_id != self.evaluation_id
            or self.risk_evidence.evaluation != self.decision.evaluation
            or self.risk_evidence.evaluation_content_hash != self.evaluation_content_hash_sha256
        ):
            raise ValueError("outcome risk evidence does not match evaluation")
        _validate_record_source_observations(self)
        _validate_reference_price(self)
        _validate_horizon_close_price(self)
        _validate_touch_intervals(self)
        _validate_outcome_status_matrix(self)
        _validate_record_resolution(self)
        if len(self.source_observation_ids) != len(self.source_observation_content_hashes):
            raise ValueError("outcome source observation IDs and hashes do not reconcile")
        _require_unique(list(self.source_observation_ids), "outcome source observation")
        for value in self.source_observation_content_hashes:
            _require_hash(value, "source_observation_content_hash")
        if tuple(item.metric for item in self.metrics) != CANONICAL_OUTCOME_METRICS:
            raise ValueError("outcome metrics must use canonical exact order")
        _validate_record_metric_formulas(self)
        _validate_record_lineage(self)
        _validate_record_metric_projection(self)
        _require_unique(list(self.reasons), "outcome reason")
        _require_unique(list(self.limitations), "outcome limitation")
        for value in (*self.reasons, *self.limitations):
            _require_sanitized_text(value, "outcome reason/limitation")
        if not self.retrospective_research_only or self.promotion_eligible:
            raise ValueError("bounded outcome records are retrospective and non-promotable")
        expected = stable_identity("opportunity-outcome", _identity_payload(self, "outcome_id"))
        if self.outcome_id != expected:
            raise ValueError("outcome record identity does not match content")


def _validate_record_source_observations(record: OutcomeRecord) -> None:
    expected_observations = tuple(
        item
        for item in record.source_series.observations
        if item.interval_end_at <= record.horizon.end_at
    )
    if record.source_observations != expected_observations:
        raise ValueError("embedded observations are not the exact horizon-local series slice")
    observation_ids = tuple(item.observation_id for item in record.source_observations)
    observation_hashes = tuple(item.content_hash() for item in record.source_observations)
    if (
        record.source_observation_ids != observation_ids
        or record.source_observation_content_hashes != observation_hashes
    ):
        raise ValueError("outcome source observation inventory does not match embedded evidence")
    previous_end: datetime | None = None
    for observation in record.source_observations:
        if (
            observation.bar.symbol != record.symbol
            or observation.bar.exchange_session_id != record.horizon.exchange_session_id
            or observation.interval_start_at <= record.decision_at
            or observation.interval_end_at > record.horizon.end_at
        ):
            raise ValueError("embedded outcome observation is outside record scope")
        if previous_end is not None and observation.interval_start_at < previous_end:
            raise ValueError("embedded outcome observations overlap or are out of order")
        previous_end = observation.interval_end_at


def _validate_record_resolution(record: OutcomeRecord) -> None:
    state = _resolve_record_state(
        decision=record.decision,
        series=record.source_series,
        policy=record.policy,
        horizon=record.horizon,
        source_frozen_at=record.source_frozen_at,
        recorded_at=record.recorded_at,
    )
    claimed = (
        record.completeness,
        record.entry_status,
        record.path_status,
        record.reasons,
    )
    expected = (
        state.completeness,
        state.entry_status,
        state.path_status,
        state.reasons,
    )
    if claimed != expected:
        raise ValueError(
            "outcome completeness, entry status, and path status do not match "
            "embedded source resolution"
        )
    expected_limitations = tuple(
        dict.fromkeys(
            (*record.source_series.limitations, "retrospective_bar_interval_resolution")
        )
    )
    if record.limitations != expected_limitations:
        raise ValueError("outcome limitations do not match embedded source resolution")


def _validate_reference_price(record: OutcomeRecord) -> None:
    values = (
        record.reference_price,
        record.reference_observation_id,
        record.reference_observation_content_hash_sha256,
    )
    if record.reference_price_kind is OutcomeReferencePriceKind.UNAVAILABLE:
        if any(value is not None for value in values):
            raise ValueError("unavailable reference price cannot carry value or lineage")
        return
    if any(value is None for value in values):
        raise ValueError("available reference price requires value and observation lineage")
    assert record.reference_price is not None
    _require_positive_decimal(record.reference_price, "reference_price")
    assert record.reference_observation_id is not None
    assert record.reference_observation_content_hash_sha256 is not None
    _require_identity(record.reference_observation_id, "reference_observation_id")
    _require_hash(
        record.reference_observation_content_hash_sha256,
        "reference_observation_content_hash_sha256",
    )
    if not record.source_observations:
        raise ValueError("available reference price requires embedded source evidence")
    reference = record.source_observations[0]
    if (
        record.reference_observation_id != reference.observation_id
        or record.reference_observation_content_hash_sha256 != reference.content_hash()
        or record.reference_price != reference.bar.open_price
    ):
        raise ValueError("reference price does not match first source observation open")


def _validate_horizon_close_price(record: OutcomeRecord) -> None:
    values = (
        record.horizon_close_price,
        record.horizon_close_observation_id,
        record.horizon_close_observation_content_hash_sha256,
    )
    if all(value is None for value in values):
        return
    if any(value is None for value in values):
        raise ValueError("horizon close price requires complete observation lineage")
    assert record.horizon_close_price is not None
    assert record.horizon_close_observation_id is not None
    assert record.horizon_close_observation_content_hash_sha256 is not None
    _require_positive_decimal(record.horizon_close_price, "horizon_close_price")
    _require_identity(record.horizon_close_observation_id, "horizon_close_observation_id")
    _require_hash(
        record.horizon_close_observation_content_hash_sha256,
        "horizon_close_observation_content_hash_sha256",
    )
    if not record.source_observations:
        raise ValueError("horizon close price requires embedded source evidence")
    close = record.source_observations[-1]
    if (
        record.horizon_close_observation_id != close.observation_id
        or record.horizon_close_observation_content_hash_sha256 != close.content_hash()
        or record.horizon_close_price != close.bar.close_price
    ):
        raise ValueError("horizon close price does not match final embedded observation close")


def _validate_record_metric_formulas(record: OutcomeRecord) -> None:
    metrics = {item.metric: item for item in record.metrics}
    sign = _direction_sign(record.direction)
    reference_metric = metrics[OutcomeMetric.REFERENCE_HORIZON_RETURN]
    if (
        record.reference_price is not None
        and record.horizon_close_price is not None
        and sign is not None
    ):
        expected_reference = (
            sign * (record.horizon_close_price - record.reference_price) / record.reference_price
        )
        if reference_metric.value != expected_reference:
            raise ValueError("reference horizon return does not match bound source prices")
    elif reference_metric.status is not OutcomeValueStatus.UNAVAILABLE:
        raise ValueError("reference horizon return requires complete bound source prices")

    gross_metric = metrics[OutcomeMetric.SIMULATED_GROSS_R]
    after_cost_metric = metrics[OutcomeMetric.SIMULATED_AFTER_COST_R]
    mfe_metric = metrics[OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R]
    mae_metric = metrics[OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R]
    stop = record.decision.evaluation.invalidation_price
    resolved = (
        record.completeness in {OutcomeCompleteness.COMPLETE, OutcomeCompleteness.PARTIAL}
        and record.path_status
        in {
            OutcomePathStatus.TARGET_FIRST,
            OutcomePathStatus.STOP_FIRST,
            OutcomePathStatus.HORIZON_EXIT,
        }
        and record.modeled_entry_price is not None
        and record.modeled_exit_price is not None
        and stop is not None
        and sign is not None
    )
    if resolved:
        assert record.modeled_entry_price is not None
        assert record.modeled_exit_price is not None
        assert stop is not None
        assert sign is not None
        expected_gross = (
            sign * (record.modeled_exit_price - record.modeled_entry_price)
            / abs(record.modeled_entry_price - stop)
        )
        if gross_metric.value != expected_gross:
            raise ValueError("simulated gross R does not match modeled geometry")
        if mfe_metric.value is None or mae_metric.value is None:
            raise ValueError("resolved filled path requires MFE and MAE evidence")
        if mfe_metric.value < max(expected_gross, Decimal("0")) or (
            mae_metric.value > min(expected_gross, Decimal("0"))
        ):
            raise ValueError("MFE/MAE do not bound the modeled gross return")
        if record.path_status is OutcomePathStatus.TARGET_FIRST and (
            mfe_metric.value != expected_gross
        ):
            raise ValueError("target-first MFE must equal target gross R")
        if record.path_status is OutcomePathStatus.STOP_FIRST and (
            mae_metric.value != expected_gross or expected_gross != Decimal("-1")
        ):
            raise ValueError("stop-first MAE must equal negative one gross R")
        expected_after_cost = _after_cost_value(
            risk=record.risk_evidence,
            modeled_entry=record.modeled_entry_price,
            modeled_stop=stop,
            modeled_exit=record.modeled_exit_price,
            sign=sign,
        )
        if expected_after_cost is None:
            if after_cost_metric.status is not OutcomeValueStatus.UNAVAILABLE:
                raise ValueError("after-cost R cannot claim mismatched risk geometry")
        elif after_cost_metric.value != expected_after_cost:
            raise ValueError("simulated after-cost R does not match bound risk evidence")
    elif any(
        item.status is not OutcomeValueStatus.UNAVAILABLE
        for item in (gross_metric, after_cost_metric, mfe_metric, mae_metric)
    ):
        raise ValueError("trade return metrics require a resolved filled path")
    _validate_time_metric_pair(
        record,
        record.target_touch_interval,
        OutcomeMetric.TIME_TO_TARGET_LOWER_BOUND,
        OutcomeMetric.TIME_TO_TARGET_UPPER_BOUND,
    )
    _validate_time_metric_pair(
        record,
        record.stop_touch_interval,
        OutcomeMetric.TIME_TO_STOP_LOWER_BOUND,
        OutcomeMetric.TIME_TO_STOP_UPPER_BOUND,
    )


def _validate_time_metric_pair(
    record: OutcomeRecord,
    touch: OutcomeTouchInterval | None,
    lower_metric: OutcomeMetric,
    upper_metric: OutcomeMetric,
) -> None:
    metrics = {item.metric: item for item in record.metrics}
    lower = metrics[lower_metric]
    upper = metrics[upper_metric]
    if record.entry_interval is None or touch is None:
        if lower.status is not OutcomeValueStatus.UNAVAILABLE or (
            upper.status is not OutcomeValueStatus.UNAVAILABLE
        ):
            raise ValueError("touch timing metrics require entry and touch intervals")
        return
    expected_lower = max(
        Decimal("0"),
        _timedelta_decimal_seconds(
            touch.interval_start_at - record.entry_interval.interval_end_at
        ),
    )
    expected_upper = _timedelta_decimal_seconds(
        touch.interval_end_at - record.entry_interval.interval_start_at
    )
    if expected_lower < 0 or expected_upper < expected_lower:
        raise ValueError("touch timing bounds have invalid chronology")
    if lower.value != expected_lower or upper.value != expected_upper:
        raise ValueError("touch timing bounds do not match interval uncertainty")


def _validate_touch_intervals(record: OutcomeRecord) -> None:
    if record.entry_status is OutcomeEntryStatus.FILLED:
        if record.modeled_entry_price is None or record.entry_interval is None:
            raise ValueError("filled outcome requires modeled entry price and interval")
        _require_positive_decimal(record.modeled_entry_price, "modeled_entry_price")
        if record.modeled_entry_price != record.decision.evaluation.entry_price:
            raise ValueError("modeled entry price must match StrategyEvaluation")
    elif record.modeled_entry_price is not None or record.entry_interval is not None:
        raise ValueError("unfilled outcome cannot carry modeled entry price or interval")
    if record.entry_status is not OutcomeEntryStatus.FILLED and any(
        item is not None
        for item in (
            record.modeled_entry_price,
            record.modeled_exit_price,
            record.entry_interval,
            record.exit_interval,
            record.target_touch_interval,
            record.stop_touch_interval,
        )
    ):
        raise ValueError("unfilled outcome cannot carry modeled trade geometry")
    if record.modeled_exit_price is not None:
        _require_positive_decimal(record.modeled_exit_price, "modeled_exit_price")
        if record.exit_interval is None:
            raise ValueError("modeled exit price requires an exit interval")
    elif record.exit_interval is not None:
        raise ValueError("exit interval requires a modeled exit price")
    for interval in (
        record.entry_interval,
        record.exit_interval,
        record.target_touch_interval,
        record.stop_touch_interval,
    ):
        if interval is not None and not (
            record.decision_at < interval.interval_start_at < interval.interval_end_at
            <= record.horizon.end_at
        ):
            raise ValueError("outcome touch interval lies outside the causal horizon")
    observation_map = {
        item.observation_id: item for item in record.source_observations
    }
    for interval in (
        record.entry_interval,
        record.exit_interval,
        record.target_touch_interval,
        record.stop_touch_interval,
    ):
        if interval is None:
            continue
        observation = observation_map.get(interval.observation_id)
        if observation is None or interval != _touch_interval(observation):
            raise ValueError("outcome touch interval does not match embedded observation")
    if record.entry_interval is not None:
        for later in (
            record.exit_interval,
            record.target_touch_interval,
            record.stop_touch_interval,
        ):
            if later is not None and (
                later.interval_start_at < record.entry_interval.interval_end_at
            ):
                raise ValueError("outcome exit/touch interval must follow the entry bar")
    evaluation = record.decision.evaluation
    if record.path_status is OutcomePathStatus.TARGET_FIRST:
        if (
            record.modeled_exit_price != evaluation.target_price
            or record.target_touch_interval != record.exit_interval
            or record.stop_touch_interval is not None
        ):
            raise ValueError("target-first path does not match evaluation/exit geometry")
    elif record.path_status is OutcomePathStatus.STOP_FIRST:
        if (
            record.modeled_exit_price != evaluation.invalidation_price
            or record.stop_touch_interval != record.exit_interval
            or record.target_touch_interval is not None
        ):
            raise ValueError("stop-first path does not match evaluation/exit geometry")
    elif record.path_status is OutcomePathStatus.HORIZON_EXIT:
        if (
            record.modeled_exit_price != record.horizon_close_price
            or record.exit_interval is None
            or record.exit_interval.observation_id != record.horizon_close_observation_id
            or record.exit_interval.observation_content_hash_sha256
            != record.horizon_close_observation_content_hash_sha256
            or record.target_touch_interval is not None
            or record.stop_touch_interval is not None
        ):
            raise ValueError("horizon-exit path does not match bound horizon close")
    elif record.path_status in {
        OutcomePathStatus.SAME_BAR_AMBIGUOUS,
        OutcomePathStatus.GAP_THROUGH_AMBIGUOUS,
    } and record.entry_status is OutcomeEntryStatus.FILLED:
        if any(
            item is not None
            for item in (
                record.modeled_exit_price,
                record.exit_interval,
                record.target_touch_interval,
                record.stop_touch_interval,
            )
        ):
            raise ValueError("post-entry ambiguous path may carry entry geometry only")
    elif record.path_status not in {
        OutcomePathStatus.SAME_BAR_AMBIGUOUS,
        OutcomePathStatus.GAP_THROUGH_AMBIGUOUS,
    } and any(
        item is not None
        for item in (
            record.modeled_entry_price,
            record.modeled_exit_price,
            record.entry_interval,
            record.exit_interval,
            record.target_touch_interval,
            record.stop_touch_interval,
        )
    ):
        raise ValueError("non-filled path cannot carry modeled trade geometry")


def _validate_record_metric_projection(record: OutcomeRecord) -> None:
    reference = (
        record.source_observations[0]
        if record.reference_price_kind is OutcomeReferencePriceKind.FIRST_POST_DECISION_OPEN
        else None
    )
    close = (
        record.source_observations[-1]
        if record.horizon_close_price is not None
        else None
    )
    try:
        path = _path_details(
            record.decision,
            record.source_observations,
            record.entry_status,
            record.path_status,
        )
    except (AssertionError, StopIteration) as exc:
        raise ValueError("outcome path cannot be reconstructed from embedded evidence") from exc
    expected = _build_metrics(
        decision=record.decision,
        risk=record.risk_evidence,
        reference=reference,
        close_observation=close,
        observations=record.source_observations,
        completeness=record.completeness,
        entry_status=record.entry_status,
        path_status=record.path_status,
        path=path,
    )
    if record.metrics != expected:
        raise ValueError("outcome metrics do not match embedded source evidence")


def _validate_outcome_status_matrix(record: OutcomeRecord) -> None:
    allowed: dict[
        OutcomeCompleteness,
        set[tuple[OutcomeEntryStatus, OutcomePathStatus]],
    ] = {
        OutcomeCompleteness.COMPLETE: {
            (OutcomeEntryStatus.FILLED, OutcomePathStatus.TARGET_FIRST),
            (OutcomeEntryStatus.FILLED, OutcomePathStatus.STOP_FIRST),
            (OutcomeEntryStatus.FILLED, OutcomePathStatus.HORIZON_EXIT),
            (OutcomeEntryStatus.NO_ENTRY, OutcomePathStatus.NO_ENTRY),
            (OutcomeEntryStatus.NOT_APPLICABLE, OutcomePathStatus.NOT_APPLICABLE),
            (OutcomeEntryStatus.UNATTAINABLE, OutcomePathStatus.UNATTAINABLE_FILL),
            (OutcomeEntryStatus.UNSUPPORTED, OutcomePathStatus.UNSUPPORTED_EVIDENCE),
        },
        OutcomeCompleteness.PARTIAL: {
            (OutcomeEntryStatus.FILLED, OutcomePathStatus.TARGET_FIRST),
            (OutcomeEntryStatus.FILLED, OutcomePathStatus.STOP_FIRST),
        },
        OutcomeCompleteness.PENDING: {
            (OutcomeEntryStatus.PENDING, OutcomePathStatus.PENDING_HORIZON),
        },
        OutcomeCompleteness.CENSORED: {
            (OutcomeEntryStatus.ENTRY_BAR_AMBIGUOUS, OutcomePathStatus.ENTRY_BAR_AMBIGUOUS),
            (OutcomeEntryStatus.GAP_THROUGH_AMBIGUOUS, OutcomePathStatus.GAP_THROUGH_AMBIGUOUS),
            (OutcomeEntryStatus.FILLED, OutcomePathStatus.SAME_BAR_AMBIGUOUS),
            (OutcomeEntryStatus.FILLED, OutcomePathStatus.GAP_THROUGH_AMBIGUOUS),
            (OutcomeEntryStatus.UNSUPPORTED, OutcomePathStatus.HALT_CENSORED),
            (OutcomeEntryStatus.UNSUPPORTED, OutcomePathStatus.CORPORATE_ACTION_CENSORED),
            (OutcomeEntryStatus.UNSUPPORTED, OutcomePathStatus.MISSING_BARS),
        },
        OutcomeCompleteness.UNAVAILABLE: {
            (OutcomeEntryStatus.UNSUPPORTED, OutcomePathStatus.UNSUPPORTED_EVIDENCE),
        },
    }
    if (record.entry_status, record.path_status) not in allowed[record.completeness]:
        raise ValueError("outcome completeness, entry status, and path status are incoherent")
    if record.completeness is not OutcomeCompleteness.COMPLETE and not record.reasons:
        raise ValueError("non-complete outcome requires an explicit reason")


def _validate_record_lineage(record: OutcomeRecord) -> None:
    inventory = dict(
        zip(
            record.source_observation_ids,
            record.source_observation_content_hashes,
            strict=True,
        )
    )
    for observation_id, content_hash in (
        (record.reference_observation_id, record.reference_observation_content_hash_sha256),
        (
            record.horizon_close_observation_id,
            record.horizon_close_observation_content_hash_sha256,
        ),
    ):
        if observation_id is not None and inventory.get(observation_id) != content_hash:
            raise ValueError("outcome price lineage is absent from source inventory")
    if record.reference_observation_id is not None and (
        not record.source_observation_ids
        or record.reference_observation_id != record.source_observation_ids[0]
    ):
        raise ValueError("reference price must bind the first source observation")
    if record.horizon_close_observation_id is not None and (
        not record.source_observation_ids
        or record.horizon_close_observation_id != record.source_observation_ids[-1]
    ):
        raise ValueError("horizon close must bind the final source observation")
    for interval in (
        record.entry_interval,
        record.exit_interval,
        record.target_touch_interval,
        record.stop_touch_interval,
    ):
        if interval is not None and (
            inventory.get(interval.observation_id)
            != interval.observation_content_hash_sha256
        ):
            raise ValueError("outcome touch lineage is absent from source inventory")
    for metric in record.metrics:
        if not set(metric.source_observation_ids).issubset(inventory):
            raise ValueError("outcome metric lineage is absent from source inventory")
        if metric.observed_at is not None and metric.observed_at > record.recorded_at:
            raise ValueError("outcome metric observation cannot postdate record")
    metrics = {item.metric: item for item in record.metrics}
    reference_ids = tuple(
        dict.fromkeys(
            item
            for item in (
                record.reference_observation_id,
                record.horizon_close_observation_id,
            )
            if item is not None
        )
    )
    _require_exact_metric_lineage(
        metrics[OutcomeMetric.REFERENCE_HORIZON_RETURN],
        reference_ids if metrics[OutcomeMetric.REFERENCE_HORIZON_RETURN].value is not None else (),
    )
    entry_id = record.entry_interval.observation_id if record.entry_interval else None
    exit_id = record.exit_interval.observation_id if record.exit_interval else None
    trade_ids = tuple(item for item in (entry_id, exit_id) if item is not None)
    for metric_name in (
        OutcomeMetric.SIMULATED_GROSS_R,
        OutcomeMetric.SIMULATED_AFTER_COST_R,
    ):
        metric = metrics[metric_name]
        _require_exact_metric_lineage(metric, trade_ids if metric.value is not None else ())
    for touch, lower_name, upper_name in (
        (
            record.target_touch_interval,
            OutcomeMetric.TIME_TO_TARGET_LOWER_BOUND,
            OutcomeMetric.TIME_TO_TARGET_UPPER_BOUND,
        ),
        (
            record.stop_touch_interval,
            OutcomeMetric.TIME_TO_STOP_LOWER_BOUND,
            OutcomeMetric.TIME_TO_STOP_UPPER_BOUND,
        ),
    ):
        touch_ids = (
            (entry_id, touch.observation_id)
            if entry_id is not None and touch is not None
            else ()
        )
        for metric_name in (lower_name, upper_name):
            metric = metrics[metric_name]
            _require_exact_metric_lineage(metric, touch_ids if metric.value is not None else ())
    favorable_ids: tuple[str, ...] = ()
    adverse_ids: tuple[str, ...] = ()
    if entry_id is not None and exit_id is not None:
        entry_index = record.source_observation_ids.index(entry_id)
        exit_index = record.source_observation_ids.index(exit_id)
        intervening_ids = record.source_observation_ids[entry_index + 1 : exit_index]
        if record.path_status is OutcomePathStatus.TARGET_FIRST:
            favorable_ids = (entry_id, exit_id)
            adverse_ids = (entry_id, *intervening_ids, exit_id)
        elif record.path_status is OutcomePathStatus.STOP_FIRST:
            favorable_ids = (entry_id, *intervening_ids, exit_id)
            adverse_ids = (entry_id, exit_id)
        else:
            favorable_ids = record.source_observation_ids[entry_index + 1 : exit_index + 1]
            adverse_ids = favorable_ids
    mfe_metric = metrics[OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R]
    mae_metric = metrics[OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R]
    _require_exact_metric_lineage(
        mfe_metric,
        favorable_ids if mfe_metric.value is not None else (),
    )
    _require_exact_metric_lineage(
        mae_metric,
        adverse_ids if mae_metric.value is not None else (),
    )
    mfe = metrics[OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R]
    mae = metrics[OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R]
    if mfe.value is not None and mfe.value < 0:
        raise ValueError("maximum favorable excursion cannot be negative")
    if mae.value is not None and mae.value > 0:
        raise ValueError("maximum adverse excursion cannot be positive")


def _require_exact_metric_lineage(
    metric: OutcomeNumericEvidence,
    expected: tuple[str, ...],
) -> None:
    if metric.source_observation_ids != expected:
        raise ValueError("outcome metric lineage does not match its canonical inputs")
