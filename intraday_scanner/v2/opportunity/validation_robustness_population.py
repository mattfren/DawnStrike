"""Exact confirmatory-unit population derived from accepted WP005-B evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from weakref import ReferenceType, ref

from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _contract_hash,
    _identity_payload,
    _require_hash,
    _require_identity,
    _require_schema,
    _require_unique,
    _require_utc,
)
from intraday_scanner.v2.opportunity.validation_metric_contracts import (
    ExecutionStressScenario,
    TradeMetricDisposition,
    ValidationMetricScopeKind,
)
from intraday_scanner.v2.opportunity.validation_metric_math import (
    _decimal_sum,
    _metric_decimal_context,
)
from intraday_scanner.v2.opportunity.validation_metric_report import (
    ValidationTradingMetricReport,
)
from intraday_scanner.v2.opportunity.validation_robustness_contracts import (
    ConfirmatoryUnit,
)

_CONTENT_HASH_CACHE: dict[int, tuple[ReferenceType[ConfirmatoryPopulation], str]] = {}


@dataclass(frozen=True)
class RobustnessSessionObservation(OutcomeContract):
    session_source_id: str
    session_content_hash_sha256: str
    session_open_at: datetime
    source_row_ids: tuple[str, ...]
    source_row_content_hashes: tuple[str, ...]
    source_run_ids: tuple[str, ...]
    source_run_content_hashes: tuple[str, ...]
    cost_3x_session_r: Decimal

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_identity(self.session_source_id, "session_source_id")
        _require_hash(self.session_content_hash_sha256, "session content hash")
        _require_utc(self.session_open_at, "session_open_at")
        if not self.source_row_ids:
            raise ValueError("confirmatory session must retain at least one exact unit row")
        _require_unique(list(self.source_row_ids), "confirmatory source row")
        if not (
            len(self.source_row_ids)
            == len(self.source_row_content_hashes)
            == len(self.source_run_ids)
            == len(self.source_run_content_hashes)
        ):
            raise ValueError("confirmatory session row lineage must align")
        for row_id in self.source_row_ids:
            _require_identity(row_id, "confirmatory source row")
        for run_id in self.source_run_ids:
            _require_identity(run_id, "confirmatory source run")
        for digest in (*self.source_row_content_hashes, *self.source_run_content_hashes):
            _require_hash(digest, "confirmatory lineage hash")
        if type(self.cost_3x_session_r) is not Decimal or not self.cost_3x_session_r.is_finite():
            raise ValueError("COST_3X session R must be a finite Decimal")


@dataclass(frozen=True)
class ConfirmatoryPopulation(OutcomeContract):
    population_id: str
    metric_report_id: str
    metric_report_content_hash_sha256: str
    metric_report: ValidationTradingMetricReport
    unit_id: str
    unit_content_hash_sha256: str
    unit: ConfirmatoryUnit
    scope_id: str
    scope_content_hash_sha256: str
    endpoint: str
    scenario: ExecutionStressScenario
    observations: tuple[RobustnessSessionObservation, ...]
    first_confirmatory_at: datetime
    session_count: int
    source_row_count: int
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.confirmatory_population.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.confirmatory_population.v1")
        _require_identity(self.population_id, "population_id")
        _require_identity(self.metric_report_id, "metric_report_id")
        _require_hash(self.metric_report_content_hash_sha256, "metric report hash")
        _require_identity(self.unit_id, "unit_id")
        _require_hash(self.unit_content_hash_sha256, "confirmatory unit hash")
        _require_identity(self.scope_id, "scope_id")
        _require_hash(self.scope_content_hash_sha256, "scope hash")
        _require_utc(self.first_confirmatory_at, "first_confirmatory_at")
        if (
            self.metric_report_id != self.metric_report.report_id
            or self.metric_report_content_hash_sha256 != self.metric_report.content_hash()
        ):
            raise ValueError("confirmatory metric report binding does not match content")
        if (
            self.unit_id != self.unit.unit_id
            or self.unit_content_hash_sha256 != self.unit.content_hash()
        ):
            raise ValueError("confirmatory unit binding does not match content")
        expected_scope, expected_observations = _derive_population(self.metric_report, self.unit)
        if (
            self.scope_id != expected_scope.scope_id
            or self.scope_content_hash_sha256 != _contract_hash(expected_scope)
        ):
            raise ValueError("confirmatory scope binding does not match content")
        if self.endpoint != "cost_3x_mean_after_cost_session_r":
            raise ValueError("confirmatory endpoint must be COST_3X mean after-cost session R")
        if self.scenario is not ExecutionStressScenario.COST_3X:
            raise ValueError("confirmatory scenario must be COST_3X")
        if self.observations != expected_observations:
            raise ValueError("confirmatory population does not recompute")
        _require_unique(
            [item.session_source_id for item in self.observations],
            "confirmatory session",
        )
        expected_counts = (
            len(expected_observations),
            sum(len(item.source_row_ids) for item in expected_observations),
        )
        if (self.session_count, self.source_row_count) != expected_counts:
            raise ValueError("confirmatory population counts do not reconcile")
        if self.first_confirmatory_at != min(
            item.session_open_at for item in expected_observations
        ):
            raise ValueError("first confirmatory timestamp does not recompute")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("confirmatory population must remain research-only")
        expected_id = stable_identity(
            "confirmatory-population", _identity_payload(self, "population_id")
        )
        if self.population_id != expected_id:
            raise ValueError("confirmatory population identity does not match content")


def build_confirmatory_population(
    report: ValidationTradingMetricReport,
    *,
    unit: ConfirmatoryUnit,
) -> ConfirmatoryPopulation:
    scope, observations = _derive_population(report, unit)
    values = {
        "metric_report_id": report.report_id,
        "metric_report_content_hash_sha256": report.content_hash(),
        "metric_report": report,
        "unit_id": unit.unit_id,
        "unit_content_hash_sha256": unit.content_hash(),
        "unit": unit,
        "scope_id": scope.scope_id,
        "scope_content_hash_sha256": _contract_hash(scope),
        "endpoint": "cost_3x_mean_after_cost_session_r",
        "scenario": ExecutionStressScenario.COST_3X,
        "observations": observations,
        "first_confirmatory_at": min(item.session_open_at for item in observations),
        "session_count": len(observations),
        "source_row_count": sum(len(item.source_row_ids) for item in observations),
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.confirmatory_population.v1",
    }
    return ConfirmatoryPopulation(
        population_id=stable_identity("confirmatory-population", values), **values
    )


def _derive_population(report: ValidationTradingMetricReport, unit: ConfirmatoryUnit):
    scopes = tuple(
        item for item in report.scopes if item.kind is ValidationMetricScopeKind.FINAL_VALIDATION
    )
    if len(scopes) != 1:
        raise ValueError("metric report must contain one final validation scope")
    scope = scopes[0]
    session_map = {
        item.session_source_id: item
        for item in report.preparation.audit_receipt.fold_collection.split_plan.corpus.sessions
    }
    row_map = {item.row_id: item for item in report.bound_rows}
    observations: list[RobustnessSessionObservation] = []
    with _metric_decimal_context(report.policy):
        for session_id in scope.session_source_ids:
            session = session_map[session_id]
            rows = tuple(
                row_map[row_id]
                for row_id in scope.row_ids
                if row_map[row_id].session_source_id == session_id
                and _row_matches_unit(row_map[row_id], unit)
            )
            if not rows:
                raise ValueError(
                    "exact confirmatory unit is not represented in every declared session"
                )
            values: list[Decimal] = []
            for row in rows:
                evidence = row.trade_evidence
                if evidence.disposition in {
                    TradeMetricDisposition.UNRESOLVED_TAKE,
                    TradeMetricDisposition.RESOLVED_FILL_COST_UNAVAILABLE,
                }:
                    raise ValueError("confirmatory population lacks complete COST_3X evidence")
                if evidence.disposition is TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE:
                    scenario = next(
                        item
                        for item in evidence.stress_scenarios
                        if item.scenario is ExecutionStressScenario.COST_3X
                    )
                    values.append(scenario.after_cost_r_unquantized)
            session_r = _decimal_sum(tuple(values), report.policy) if values else Decimal("0")
            observations.append(
                RobustnessSessionObservation(
                    session_source_id=session_id,
                    session_content_hash_sha256=session.content_hash(),
                    session_open_at=session.session_open_at,
                    source_row_ids=tuple(item.row_id for item in rows),
                    source_row_content_hashes=tuple(item.row_content_hash_sha256 for item in rows),
                    source_run_ids=tuple(item.run_id for item in rows),
                    source_run_content_hashes=tuple(item.run_content_hash_sha256 for item in rows),
                    cost_3x_session_r=session_r,
                )
            )
    if not observations:
        raise ValueError("confirmatory population cannot be empty")
    return scope, tuple(observations)


def _row_matches_unit(row, unit: ConfirmatoryUnit) -> bool:
    evaluation = row.outcome.decision.evaluation
    return (
        evaluation.strategy_id == unit.strategy_id
        and evaluation.strategy_version == unit.strategy_version
        and evaluation.direction is unit.direction
    )


def _confirmatory_population_content_hash(population: ConfirmatoryPopulation) -> str:
    """Memoize the expensive hash of an immutable, already-self-verified body."""

    key = id(population)
    cached = _CONTENT_HASH_CACHE.get(key)
    if cached is not None and cached[0]() is population:
        return cached[1]
    digest = population.content_hash()

    def discard(_reference: ReferenceType[ConfirmatoryPopulation]) -> None:
        _CONTENT_HASH_CACHE.pop(key, None)

    _CONTENT_HASH_CACHE[key] = (ref(population, discard), digest)
    return digest


__all__ = [
    "ConfirmatoryPopulation",
    "RobustnessSessionObservation",
    "build_confirmatory_population",
]
