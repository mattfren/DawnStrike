"""Pure construction of a frozen, all-evaluation validation corpus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeCompleteness,
    OutcomeContract,
    OutcomeHorizon,
    _identity_payload,
    _require_identity,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
    _require_utc,
)
from intraday_scanner.v2.opportunity.outcome_persistence import CurrentOutcomeReplay
from intraday_scanner.v2.opportunity.outcome_records import OutcomeRecord
from intraday_scanner.v2.opportunity.validation_contracts import (
    SurvivorshipEvidenceStatus,
    ValidationCorpusPolicy,
    ValidationCorpusStatus,
    ValidationHorizonSelectionKind,
    ValidationSurvivorshipEvidence,
)


@dataclass(frozen=True)
class _SelectedOutcomeHorizonBinding:
    replay_id: str
    replay_content_hash_sha256: str
    batch_id: str
    batch_content_hash_sha256: str
    horizon_id: str
    horizon_content_hash_sha256: str
    horizon: OutcomeHorizon


@dataclass(frozen=True)
class _ValidationCorpusRow:
    row_id: str
    session_source_id: str
    replay_id: str
    replay_content_hash_sha256: str
    run_id: str
    run_content_hash_sha256: str
    evaluation_id: str
    evaluation_content_hash_sha256: str
    decision_id: str
    decision_content_hash_sha256: str
    outcome_id: str
    outcome_content_hash_sha256: str
    horizon_id: str
    horizon_content_hash_sha256: str
    decision_at: datetime
    label_end_at: datetime
    required_available_at: datetime
    decision_value: str
    completeness: str
    entry_status: str
    path_status: str


@dataclass(frozen=True)
class ValidationSessionSource(OutcomeContract):
    session_source_id: str
    exchange_session_id: str
    session_open_at: datetime
    session_close_at: datetime
    current_outcome_replays: tuple[CurrentOutcomeReplay, ...]
    survivorship_evidence: tuple[ValidationSurvivorshipEvidence, ...]
    replay_count: int
    run_count: int
    evaluation_count: int
    survivorship_status: SurvivorshipEvidenceStatus
    limitations: tuple[str, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.validation_session_source.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.validation_session_source.v1")
        _require_identity(self.session_source_id, "session_source_id")
        _require_sanitized_text(self.exchange_session_id, "exchange_session_id")
        _require_utc(self.session_open_at, "session_open_at")
        _require_utc(self.session_close_at, "session_close_at")
        if self.session_open_at >= self.session_close_at:
            raise ValueError("validation session must have positive duration")
        if not self.current_outcome_replays:
            raise ValueError("validation session source requires at least one replay")
        expected_replay_order = tuple(
            sorted(
                self.current_outcome_replays,
                key=lambda item: (
                    item.pipeline_result.decision_at,
                    item.pipeline_result.run_id,
                    item.replay_id,
                ),
            )
        )
        if self.current_outcome_replays != expected_replay_order:
            raise ValueError("validation session replays must use canonical order")
        _require_unique(
            [item.replay_id for item in self.current_outcome_replays], "current replay"
        )
        _require_unique(
            [item.pipeline_result.run_id for item in self.current_outcome_replays],
            "pipeline run",
        )
        for replay in self.current_outcome_replays:
            _validate_replay_session(
                replay,
                exchange_session_id=self.exchange_session_id,
                session_open_at=self.session_open_at,
                session_close_at=self.session_close_at,
            )
        expected_evidence = _ordered_survivorship(
            self.current_outcome_replays, self.survivorship_evidence
        )
        if self.survivorship_evidence != expected_evidence:
            raise ValueError("session survivorship evidence does not reconcile replays")
        expected_status = _aggregate_survivorship(self.survivorship_evidence)
        if self.survivorship_status is not expected_status:
            raise ValueError("session survivorship status does not recompute")
        expected_counts = (
            len(self.current_outcome_replays),
            len(self.current_outcome_replays),
            sum(
                len(item.pipeline_result.preparation.evaluations)
                for item in self.current_outcome_replays
            ),
        )
        if (self.replay_count, self.run_count, self.evaluation_count) != expected_counts:
            raise ValueError("validation session counts do not reconcile")
        expected_limitations = _session_limitations(expected_status)
        if self.limitations != expected_limitations:
            raise ValueError("validation session limitations do not recompute")
        if not self.research_only:
            raise ValueError("validation session source must remain research-only")
        expected = stable_identity(
            "validation-session-source", _identity_payload(self, "session_source_id")
        )
        if self.session_source_id != expected:
            raise ValueError("validation session source identity does not match content")


@dataclass(frozen=True)
class ValidationCorpus(OutcomeContract):
    corpus_id: str
    policy: ValidationCorpusPolicy
    frozen_at: datetime
    sessions: tuple[ValidationSessionSource, ...]
    selected_horizons: tuple[_SelectedOutcomeHorizonBinding, ...]
    rows: tuple[_ValidationCorpusRow, ...]
    status: ValidationCorpusStatus
    session_count: int
    replay_count: int
    row_count: int
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.validation_corpus.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.validation_corpus.v1")
        _require_identity(self.corpus_id, "corpus_id")
        _require_utc(self.frozen_at, "frozen_at")
        expected_sessions = tuple(
            sorted(
                self.sessions,
                key=lambda item: (
                    item.session_open_at,
                    item.session_close_at,
                    item.exchange_session_id,
                ),
            )
        )
        if self.sessions != expected_sessions:
            raise ValueError("validation sessions must use chronological canonical order")
        _require_unique([item.session_source_id for item in self.sessions], "session source")
        _require_unique([item.exchange_session_id for item in self.sessions], "exchange session")
        for previous, current in zip(self.sessions, self.sessions[1:], strict=False):
            if previous.session_close_at > current.session_open_at:
                raise ValueError("validation sessions overlap")
        expected_horizons, expected_rows = _derive_corpus_projections(
            self.sessions, self.policy
        )
        if self.selected_horizons != expected_horizons:
            raise ValueError("selected outcome horizons do not recompute")
        if self.rows != expected_rows:
            raise ValueError("validation corpus rows do not recompute")
        if any(item.required_available_at > self.frozen_at for item in self.rows):
            raise ValueError("validation corpus freezes before required source availability")
        evidence_times = (
            item_time
            for session in self.sessions
            for evidence in session.survivorship_evidence
            for item_time in _survivorship_times(evidence)
        )
        if any(item > self.frozen_at for item in evidence_times):
            raise ValueError("validation corpus freezes before survivorship evidence")
        expected_status = _corpus_status(self.sessions, self.rows)
        if self.status is not expected_status:
            raise ValueError("validation corpus status does not recompute")
        expected_counts = (
            len(self.sessions),
            sum(len(item.current_outcome_replays) for item in self.sessions),
            len(self.rows),
        )
        if (self.session_count, self.replay_count, self.row_count) != expected_counts:
            raise ValueError("validation corpus counts do not reconcile")
        expected_limitations = _corpus_limitations(self.sessions, self.rows)
        if self.limitations != expected_limitations:
            raise ValueError("validation corpus limitations do not recompute")
        _validate_global_content_identity(self.sessions)
        if not self.research_only or self.promotion_eligible:
            raise ValueError("validation corpus must remain research-only and nonpromotable")
        expected = stable_identity("validation-corpus", _identity_payload(self, "corpus_id"))
        if self.corpus_id != expected:
            raise ValueError("validation corpus identity does not match content")


def build_validation_session_sources(
    *,
    current_replays: tuple[CurrentOutcomeReplay, ...],
    survivorship_evidence: tuple[ValidationSurvivorshipEvidence, ...],
) -> tuple[ValidationSessionSource, ...]:
    replay_groups: dict[
        tuple[str, datetime, datetime], list[CurrentOutcomeReplay]
    ] = {}
    for replay in current_replays:
        session_key = _replay_session_key(replay)
        replay_groups.setdefault(session_key, []).append(replay)
    evidence_by_snapshot = {
        item.universe_snapshot_id: item for item in survivorship_evidence
    }
    if len(evidence_by_snapshot) != len(survivorship_evidence):
        raise ValueError("duplicate survivorship evidence for universe snapshot")
    used_evidence: set[str] = set()
    result: list[ValidationSessionSource] = []
    for (session_id, session_open, session_close), replays in sorted(
        replay_groups.items(), key=lambda item: (item[0][1], item[0][2], item[0][0])
    ):
        ordered_replays = tuple(
            sorted(
                replays,
                key=lambda item: (
                    item.pipeline_result.decision_at,
                    item.pipeline_result.run_id,
                    item.replay_id,
                ),
            )
        )
        snapshot_ids = tuple(
            sorted(
                {
                    item.pipeline_result.universe_snapshot_id
                    for item in ordered_replays
                }
            )
        )
        try:
            evidence = tuple(evidence_by_snapshot[item] for item in snapshot_ids)
        except KeyError as exc:
            raise ValueError("missing survivorship evidence for replay universe") from exc
        used_evidence.update(snapshot_ids)
        status = _aggregate_survivorship(evidence)
        values = {
            "exchange_session_id": session_id,
            "session_open_at": session_open,
            "session_close_at": session_close,
            "current_outcome_replays": ordered_replays,
            "survivorship_evidence": evidence,
            "replay_count": len(ordered_replays),
            "run_count": len(ordered_replays),
            "evaluation_count": sum(
                len(item.pipeline_result.preparation.evaluations)
                for item in ordered_replays
            ),
            "survivorship_status": status,
            "limitations": _session_limitations(status),
            "research_only": True,
            "schema_version": "v2.opportunity.validation_session_source.v1",
        }
        result.append(
            ValidationSessionSource(
                session_source_id=stable_identity("validation-session-source", values),
                exchange_session_id=session_id,
                session_open_at=session_open,
                session_close_at=session_close,
                current_outcome_replays=ordered_replays,
                survivorship_evidence=evidence,
                replay_count=len(ordered_replays),
                run_count=len(ordered_replays),
                evaluation_count=sum(
                    len(item.pipeline_result.preparation.evaluations)
                    for item in ordered_replays
                ),
                survivorship_status=status,
                limitations=_session_limitations(status),
            )
        )
    if set(evidence_by_snapshot) != used_evidence:
        raise ValueError("survivorship evidence contains an unknown universe snapshot")
    return tuple(result)


def build_validation_corpus(
    *,
    current_replays: tuple[CurrentOutcomeReplay, ...],
    survivorship_evidence: tuple[ValidationSurvivorshipEvidence, ...],
    policy: ValidationCorpusPolicy,
    frozen_at: datetime,
) -> ValidationCorpus:
    sessions = build_validation_session_sources(
        current_replays=current_replays,
        survivorship_evidence=survivorship_evidence,
    )
    horizons, rows = _derive_corpus_projections(sessions, policy)
    status = _corpus_status(sessions, rows)
    values = {
        "policy": policy,
        "frozen_at": frozen_at,
        "sessions": sessions,
        "selected_horizons": horizons,
        "rows": rows,
        "status": status,
        "session_count": len(sessions),
        "replay_count": len(current_replays),
        "row_count": len(rows),
        "limitations": _corpus_limitations(sessions, rows),
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.validation_corpus.v1",
    }
    return ValidationCorpus(
        corpus_id=stable_identity("validation-corpus", values),
        policy=policy,
        frozen_at=frozen_at,
        sessions=sessions,
        selected_horizons=horizons,
        rows=rows,
        status=status,
        session_count=len(sessions),
        replay_count=len(current_replays),
        row_count=len(rows),
        limitations=_corpus_limitations(sessions, rows),
    )


def _replay_session_key(replay: CurrentOutcomeReplay) -> tuple[str, datetime, datetime]:
    horizons = replay.outcome_batch.horizons
    if not horizons:
        raise ValueError("current replay without horizons cannot prove a validation session")
    keys = {
        (item.exchange_session_id, item.session_open_at, item.session_close_at)
        for item in horizons
    }
    if len(keys) != 1:
        raise ValueError("current replay mixes exchange sessions")
    return next(iter(keys))


def _validate_replay_session(
    replay: CurrentOutcomeReplay,
    *,
    exchange_session_id: str,
    session_open_at: datetime,
    session_close_at: datetime,
) -> None:
    if _replay_session_key(replay) != (
        exchange_session_id,
        session_open_at,
        session_close_at,
    ):
        raise ValueError("current outcome replay does not match validation session")
    if not session_open_at <= replay.pipeline_result.decision_at < session_close_at:
        raise ValueError("pipeline decision does not lie inside outcome session")


def _ordered_survivorship(
    replays: tuple[CurrentOutcomeReplay, ...],
    evidence: tuple[ValidationSurvivorshipEvidence, ...],
) -> tuple[ValidationSurvivorshipEvidence, ...]:
    snapshots = {
        item.pipeline_result.universe_snapshot_id: (
            item.pipeline_result.universe_snapshot_content_hash,
            item.pipeline_result.preparation.universe_snapshot,
        )
        for item in replays
    }
    if len(snapshots) != len(
        {item.pipeline_result.universe_snapshot_id for item in replays}
    ):
        raise ValueError("duplicate universe identity with different content")
    evidence_map = {item.universe_snapshot_id: item for item in evidence}
    if len(evidence_map) != len(evidence) or set(evidence_map) != set(snapshots):
        raise ValueError("survivorship evidence must exactly cover replay universes")
    for snapshot_id, (snapshot_hash, snapshot) in snapshots.items():
        item = evidence_map[snapshot_id]
        if (
            item.universe_snapshot_content_hash_sha256 != snapshot_hash
            or item.universe_snapshot != snapshot
        ):
            raise ValueError("survivorship evidence binds a different universe body")
    return tuple(evidence_map[item] for item in sorted(evidence_map))


def _aggregate_survivorship(
    evidence: tuple[ValidationSurvivorshipEvidence, ...],
) -> SurvivorshipEvidenceStatus:
    statuses = {item.status for item in evidence}
    if SurvivorshipEvidenceStatus.UNAVAILABLE in statuses:
        return SurvivorshipEvidenceStatus.UNAVAILABLE
    if SurvivorshipEvidenceStatus.UNKNOWN in statuses:
        return SurvivorshipEvidenceStatus.UNKNOWN
    if SurvivorshipEvidenceStatus.CURRENT_MEMBERSHIP_PROXY in statuses:
        return SurvivorshipEvidenceStatus.CURRENT_MEMBERSHIP_PROXY
    return SurvivorshipEvidenceStatus.POINT_IN_TIME


def _derive_corpus_projections(
    sessions: tuple[ValidationSessionSource, ...], policy: ValidationCorpusPolicy
) -> tuple[tuple[_SelectedOutcomeHorizonBinding, ...], tuple[_ValidationCorpusRow, ...]]:
    horizon_bindings: list[_SelectedOutcomeHorizonBinding] = []
    rows: list[_ValidationCorpusRow] = []
    for session in sessions:
        for replay in session.current_outcome_replays:
            batch = replay.outcome_batch
            if (
                batch.policy.policy_id != policy.outcome_label_policy_id
                or batch.policy.content_hash()
                != policy.outcome_label_policy_content_hash_sha256
            ):
                raise ValueError("outcome batch label policy does not match corpus policy")
            matching = tuple(
                item for item in batch.horizons if _horizon_matches(item, policy)
            )
            if len(matching) != 1:
                raise ValueError("outcome replay must contain exactly one selected horizon")
            horizon = matching[0]
            binding = _SelectedOutcomeHorizonBinding(
                replay_id=replay.replay_id,
                replay_content_hash_sha256=replay.content_hash(),
                batch_id=batch.batch_id,
                batch_content_hash_sha256=batch.content_hash(),
                horizon_id=horizon.horizon_id,
                horizon_content_hash_sha256=horizon.content_hash(),
                horizon=horizon,
            )
            horizon_bindings.append(binding)
            by_evaluation = {
                item.evaluation_id: item
                for item in batch.outcomes
                if item.horizon_id == horizon.horizon_id
            }
            evaluations = replay.pipeline_result.preparation.evaluations
            if len(by_evaluation) != len(evaluations):
                raise ValueError("selected horizon does not cover every evaluation")
            for evaluation in evaluations:
                try:
                    outcome = by_evaluation[evaluation.evaluation_id]
                except KeyError as exc:
                    raise ValueError("selected horizon is missing an evaluation") from exc
                rows.append(_build_row(session, replay, horizon, outcome))
    return tuple(horizon_bindings), tuple(rows)


def _horizon_matches(horizon: OutcomeHorizon, policy: ValidationCorpusPolicy) -> bool:
    if horizon.kind is not policy.accepted_horizon_kind:
        return False
    if policy.horizon_kind is ValidationHorizonSelectionKind.ELAPSED_SECONDS:
        return horizon.elapsed_seconds == policy.elapsed_seconds
    return horizon.elapsed_seconds is None and horizon.end_at == horizon.session_close_at


def _build_row(
    session: ValidationSessionSource,
    replay: CurrentOutcomeReplay,
    horizon: OutcomeHorizon,
    outcome: OutcomeRecord,
) -> _ValidationCorpusRow:
    required_at = _required_available_at(session, replay, outcome)
    values = {
        "session_source_id": session.session_source_id,
        "replay_id": replay.replay_id,
        "replay_content_hash_sha256": replay.content_hash(),
        "run_id": replay.pipeline_result.run_id,
        "run_content_hash_sha256": replay.pipeline_result.content_hash(),
        "evaluation_id": outcome.evaluation_id,
        "evaluation_content_hash_sha256": outcome.evaluation_content_hash_sha256,
        "decision_id": outcome.decision_id,
        "decision_content_hash_sha256": outcome.decision_content_hash_sha256,
        "outcome_id": outcome.outcome_id,
        "outcome_content_hash_sha256": outcome.content_hash(),
        "horizon_id": horizon.horizon_id,
        "horizon_content_hash_sha256": horizon.content_hash(),
        "decision_at": outcome.decision_at,
        "label_end_at": horizon.end_at,
        "required_available_at": required_at,
        "decision_value": outcome.decision_value.value,
        "completeness": outcome.completeness.value,
        "entry_status": outcome.entry_status.value,
        "path_status": outcome.path_status.value,
    }
    return _ValidationCorpusRow(
        row_id=stable_identity("validation-corpus-row", values),
        session_source_id=session.session_source_id,
        replay_id=replay.replay_id,
        replay_content_hash_sha256=replay.content_hash(),
        run_id=replay.pipeline_result.run_id,
        run_content_hash_sha256=replay.pipeline_result.content_hash(),
        evaluation_id=outcome.evaluation_id,
        evaluation_content_hash_sha256=outcome.evaluation_content_hash_sha256,
        decision_id=outcome.decision_id,
        decision_content_hash_sha256=outcome.decision_content_hash_sha256,
        outcome_id=outcome.outcome_id,
        outcome_content_hash_sha256=outcome.content_hash(),
        horizon_id=horizon.horizon_id,
        horizon_content_hash_sha256=horizon.content_hash(),
        decision_at=outcome.decision_at,
        label_end_at=horizon.end_at,
        required_available_at=required_at,
        decision_value=outcome.decision_value.value,
        completeness=outcome.completeness.value,
        entry_status=outcome.entry_status.value,
        path_status=outcome.path_status.value,
    )


def _required_available_at(
    session: ValidationSessionSource,
    replay: CurrentOutcomeReplay,
    outcome: OutcomeRecord,
) -> datetime:
    times = [
        outcome.horizon.end_at,
        replay.run_persistence_receipt.recorded_at,
        replay.outcome_batch.recorded_at,
        replay.outcome_batch.source_dataset.frozen_at,
        replay.outcome_persistence_receipt.persisted_at,
        outcome.recorded_at,
        outcome.source_frozen_at,
        *(item.persisted_at for item in replay.full_chain),
        *(item.available_at for item in outcome.source_observations),
    ]
    receipt = outcome.source_series.coverage_receipt
    times.extend(
        (
            receipt.source_metadata.fetched_at,
            receipt.created_at or receipt.source_metadata.fetched_at,
            *(
                item.source_metadata.fetched_at
                for item in outcome.source_series.market_status_intervals
            ),
            *(
                item.source_metadata.fetched_at
                for item in outcome.source_series.corporate_actions
            ),
        )
    )
    if outcome.risk_evidence is not None:
        times.extend(
            item.observed_at
            for item in outcome.risk_evidence.metrics
            if item.observed_at is not None
        )
        times.extend(
            item.observed_at for item in outcome.risk_evidence.capability_receipts
        )
        times.extend(
            (
                outcome.risk_evidence.halt_evidence.observed_at,
                outcome.risk_evidence.corporate_action_evidence.observed_at,
            )
        )
    times.extend(
        fact_time
        for evidence in session.survivorship_evidence
        for fact_time in _survivorship_times(evidence)
    )
    return max(times)


def _survivorship_times(evidence: ValidationSurvivorshipEvidence) -> tuple[datetime, ...]:
    values: list[datetime] = []
    if evidence.membership_body is not None:
        values.extend(
            (
                evidence.membership_body.membership_effective_at,
                evidence.membership_body.observed_at,
            )
        )
    values.extend(
        fact_time
        for item in evidence.source_artifacts
        for fact_time in (item.observed_at, item.fetched_at)
    )
    return tuple(values)


def _session_limitations(status: SurvivorshipEvidenceStatus) -> tuple[str, ...]:
    if status is SurvivorshipEvidenceStatus.POINT_IN_TIME:
        return ()
    return (f"survivorship_{status.value}",)


def _corpus_status(
    sessions: tuple[ValidationSessionSource, ...], rows: tuple[_ValidationCorpusRow, ...]
) -> ValidationCorpusStatus:
    if not rows:
        return ValidationCorpusStatus.EMPTY
    if any(
        item.survivorship_status is not SurvivorshipEvidenceStatus.POINT_IN_TIME
        for item in sessions
    ):
        return ValidationCorpusStatus.EXTERNAL_DATA_BLOCKED
    if any(item.completeness != OutcomeCompleteness.COMPLETE.value for item in rows):
        return ValidationCorpusStatus.INCOMPLETE
    return ValidationCorpusStatus.AVAILABLE


def _corpus_limitations(
    sessions: tuple[ValidationSessionSource, ...], rows: tuple[_ValidationCorpusRow, ...]
) -> tuple[str, ...]:
    values = {
        limitation for session in sessions for limitation in session.limitations
    }
    if any(item.completeness != OutcomeCompleteness.COMPLETE.value for item in rows):
        values.add("outcome_population_contains_incomplete_labels")
    if not rows:
        values.add("validation_corpus_empty")
    return tuple(sorted(values))


def _validate_global_content_identity(
    sessions: tuple[ValidationSessionSource, ...],
) -> None:
    seen: dict[tuple[str, str], str] = {}
    for session in sessions:
        for replay in session.current_outcome_replays:
            objects = (
                ("replay", replay.replay_id, replay.content_hash()),
                ("run", replay.pipeline_result.run_id, replay.pipeline_result.content_hash()),
                *(
                    ("evaluation", item.evaluation_id, item.content_hash())
                    for item in replay.pipeline_result.preparation.evaluations
                ),
                *(
                    ("horizon", item.horizon_id, item.content_hash())
                    for item in replay.outcome_batch.horizons
                ),
                (
                    "universe",
                    replay.pipeline_result.universe_snapshot_id,
                    replay.pipeline_result.universe_snapshot_content_hash,
                ),
            )
            for family, identity, content_hash in objects:
                key = (family, identity)
                previous = seen.setdefault(key, content_hash)
                if previous != content_hash:
                    raise ValueError(f"duplicate {family} identity has different content")


__all__ = [
    "ValidationCorpus",
    "ValidationSessionSource",
    "build_validation_corpus",
    "build_validation_session_sources",
]
