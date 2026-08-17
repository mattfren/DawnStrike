from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.outcomes import (
    build_outcome_bar_evidence,
    build_outcome_observation_series,
)
from intraday_scanner.v2.opportunity.validation_audit import (
    ChronologicalValidationPreparationReceipt,
    TimestampLeakageAuditReceipt,
    audit_validation_timestamps,
    build_chronological_validation_preparation,
)
from intraday_scanner.v2.opportunity.validation_contracts import (
    HoldoutAccessStatus,
    HoldoutIntegrityStatus,
    LeakageAuditStatus,
    LeakageCheckStatus,
    SplitPlanStatus,
    SplitRole,
    SurvivorshipEvidenceStatus,
    ValidationCorpusPolicy,
    ValidationCorpusStatus,
    ValidationHorizonSelectionKind,
    build_holdout_access_evidence,
    build_validation_corpus_policy,
    build_validation_membership_body,
    build_validation_source_artifact,
    build_validation_split_policy,
    build_validation_survivorship_evidence,
    validation_membership_normalized_hash,
)
from intraday_scanner.v2.opportunity.validation_corpus import (
    ValidationCorpus,
    build_validation_corpus,
)
from intraday_scanner.v2.opportunity.validation_split import (
    ChronologicalSplitPlan,
    WalkForwardFoldCollection,
    build_chronological_split_plan,
    build_expanding_walk_forward_folds,
)
from tests import test_opportunity_outcomes as outcome_fixtures
from tests import test_opportunity_pipeline as pipeline_fixtures
from tests.test_opportunity_missed import (
    _current_outcome_replay,
    _stored_replay_for_batch,
)

UTC = timezone.utc
_BASE_NOW = pipeline_fixtures.NOW
_BASE_DATASET = pipeline_fixtures._two_candidate_dataset()
_BASE_SOURCE_SERIES = outcome_fixtures._source_series
_BASE_HORIZON_BUILDER = outcome_fixtures.build_outcome_horizon
_SHIFTED_REPLAY_CACHE: dict[tuple[int, int], object] = {}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _survivorship(replay, *, status=SurvivorshipEvidenceStatus.POINT_IN_TIME):
    snapshot = replay.pipeline_result.preparation.universe_snapshot
    observed_at = snapshot.as_of.astimezone(UTC)
    effective_at = observed_at
    if status is SurvivorshipEvidenceStatus.CURRENT_MEMBERSHIP_PROXY:
        effective_at = observed_at + timedelta(days=1)
    normalized_hash = validation_membership_normalized_hash(
        universe_snapshot=snapshot,
        membership_effective_at=effective_at,
        observed_at=observed_at,
        provider="fixture-membership-provider",
        source_identity="historical-membership-fixture",
        source_version="fixture-v1",
        method="normalized point-in-time constituent snapshot",
    )
    artifact = build_validation_source_artifact(
        raw_artifact_hash_sha256=_hash("validation-membership-raw"),
        normalized_artifact_hash_sha256=normalized_hash,
        provider="fixture-membership-provider",
        source_identity="historical-membership-fixture",
        source_version="fixture-v1",
        method="normalized point-in-time constituent snapshot",
        observed_at=observed_at,
        fetched_at=observed_at,
    )
    body = build_validation_membership_body(
        universe_snapshot=snapshot,
        membership_effective_at=effective_at,
        observed_at=observed_at,
        provider="fixture-membership-provider",
        source_identity="historical-membership-fixture",
        source_version="fixture-v1",
        method="normalized point-in-time constituent snapshot",
        source_artifacts=(artifact,),
    )
    return build_validation_survivorship_evidence(
        universe_snapshot=snapshot,
        status=status,
        membership_body=body,
        source_artifacts=(artifact,),
    )


def _survivorship_fetched_at(replay, fetched_at):
    original = _survivorship(replay)
    assert original.membership_body is not None
    body = original.membership_body
    source = original.source_artifacts[0]
    artifact = build_validation_source_artifact(
        raw_artifact_hash_sha256=source.raw_artifact_hash_sha256,
        normalized_artifact_hash_sha256=body.normalized_content_hash_sha256,
        provider=body.provider,
        source_identity=body.source_identity,
        source_version=body.source_version,
        method=body.method,
        observed_at=body.observed_at,
        fetched_at=fetched_at,
    )
    rebuilt_body = build_validation_membership_body(
        universe_snapshot=original.universe_snapshot,
        membership_effective_at=body.membership_effective_at,
        observed_at=body.observed_at,
        provider=body.provider,
        source_identity=body.source_identity,
        source_version=body.source_version,
        method=body.method,
        source_artifacts=(artifact,),
    )
    return build_validation_survivorship_evidence(
        universe_snapshot=original.universe_snapshot,
        status=SurvivorshipEvidenceStatus.POINT_IN_TIME,
        membership_body=rebuilt_body,
        source_artifacts=(artifact,),
    )


def _policy(replay) -> ValidationCorpusPolicy:
    label_policy = replay.outcome_batch.policy
    return build_validation_corpus_policy(
        policy_version="wp005-a-v1",
        horizon_kind=ValidationHorizonSelectionKind.ELAPSED_SECONDS,
        elapsed_seconds=301,
        outcome_label_policy_id=label_policy.policy_id,
        outcome_label_policy_content_hash_sha256=label_policy.content_hash(),
    )


def _corpus(*, status=SurvivorshipEvidenceStatus.POINT_IN_TIME):
    replay = _current_outcome_replay()
    evidence = _survivorship(replay, status=status)
    frozen_at = max(
        replay.outcome_batch.recorded_at,
        replay.outcome_persistence_receipt.persisted_at,
        evidence.membership_body.membership_effective_at
        if evidence.membership_body is not None
        else replay.outcome_batch.recorded_at,
        *(item.fetched_at for item in evidence.source_artifacts),
    ) + timedelta(seconds=1)
    return build_validation_corpus(
        current_replays=(replay,),
        survivorship_evidence=(evidence,),
        policy=_policy(replay),
        frozen_at=frozen_at,
    )


def _shifted_replay(monkeypatch: pytest.MonkeyPatch, *, seconds: int, ordinal: int):
    cache_key = (seconds, ordinal)
    if cache_key in _SHIFTED_REPLAY_CACHE:
        return _SHIFTED_REPLAY_CACHE[cache_key]
    delta = timedelta(seconds=seconds)
    decision_at = _BASE_NOW + delta
    session_id = f"XNYS-validation-{ordinal:02d}"
    shifted_bars = {
        symbol: tuple(
            replace(
                bar,
                timestamp=bar.timestamp + delta,
                exchange_session_id=session_id,
            )
            for bar in bars
        )
        for symbol, bars in _BASE_DATASET.bars_by_symbol.items()
    }
    shifted_dataset = replace(
        _BASE_DATASET,
        dataset_id=f"pipeline-validation-{ordinal:02d}",
        bars_by_symbol=shifted_bars,
    )

    def shifted_source(result, symbol: str, *, missing_index: int | None = None):
        original = _BASE_SOURCE_SERIES(result, symbol, missing_index=missing_index)
        metadata = replace(
            original.coverage_receipt.source_metadata,
            exchange_session_id=session_id,
            raw_artifact_hash_sha256=_hash(f"raw-{symbol}-{ordinal}"),
            normalized_artifact_hash_sha256=_hash(f"normalized-{symbol}-{ordinal}"),
        )
        observations = tuple(
            build_outcome_bar_evidence(
                bar=replace(
                    item.bar,
                    exchange_session_id=session_id,
                    source_metadata=metadata,
                ),
                interval_start_at=item.interval_start_at,
                interval_end_at=item.interval_end_at,
                available_at=item.available_at,
            )
            for item in original.observations
        )
        coverage = replace(
            original.coverage_receipt,
            coverage_receipt_id=f"coverage-{symbol.lower()}-{ordinal:02d}",
            market_date=decision_at.date().isoformat(),
            exchange_session_id=session_id,
            source_metadata=metadata,
        )
        return build_outcome_observation_series(
            symbol=symbol,
            exchange_session_id=session_id,
            decision_at=result.decision_at,
            requested_through_at=original.requested_through_at,
            coverage_receipt=coverage,
            observations=observations,
            source_identity="fixture_outcome_source",
            method="retained_post_decision_minute_bars",
        )

    def shifted_horizon(**kwargs):
        decision_utc = kwargs["decision_at"].astimezone(UTC)
        return _BASE_HORIZON_BUILDER(
            **{
                **kwargs,
                "exchange_session_id": session_id,
                "session_open_at": decision_utc,
                "session_close_at": decision_utc + timedelta(seconds=302),
            }
        )

    monkeypatch.setattr(pipeline_fixtures, "NOW", decision_at)
    monkeypatch.setattr(
        pipeline_fixtures, "_two_candidate_dataset", lambda: shifted_dataset
    )
    monkeypatch.setattr(outcome_fixtures, "_source_series", shifted_source)
    monkeypatch.setattr(outcome_fixtures, "build_outcome_horizon", shifted_horizon)
    replay = _stored_replay_for_batch(outcome_fixtures._batch())
    _SHIFTED_REPLAY_CACHE[cache_key] = replay
    return replay


def _multi_session_corpus(
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int,
    spacing_seconds: int = 304,
    status: SurvivorshipEvidenceStatus = SurvivorshipEvidenceStatus.POINT_IN_TIME,
):
    replays = tuple(
        _shifted_replay(
            monkeypatch,
            seconds=index * spacing_seconds,
            ordinal=index + 1,
        )
        for index in range(count)
    )
    evidence = tuple(_survivorship(item, status=status) for item in replays)
    frozen_at = max(
        *(
            item.outcome_persistence_receipt.persisted_at for item in replays
        ),
        *(
            item.membership_body.membership_effective_at
            for item in evidence
            if item.membership_body is not None
        ),
    ) + timedelta(seconds=1)
    return build_validation_corpus(
        current_replays=replays,
        survivorship_evidence=evidence,
        policy=_policy(replays[0]),
        frozen_at=frozen_at,
    )


def test_validation_corpus_reconciles_exact_current_replay_and_round_trips() -> None:
    replay = _current_outcome_replay()
    corpus = _corpus()

    assert corpus.status is ValidationCorpusStatus.AVAILABLE
    assert corpus.session_count == corpus.replay_count == 1
    assert corpus.row_count == len(replay.pipeline_result.preparation.evaluations)
    assert tuple(item.evaluation_id for item in corpus.rows) == tuple(
        item.evaluation_id for item in replay.pipeline_result.preparation.evaluations
    )
    assert {item.decision_value for item in corpus.rows} == {
        item.decision_value.value for item in replay.outcome_batch.outcomes
    }
    assert ValidationCorpus.from_json(corpus.to_json()) == corpus


def test_point_in_time_requires_normalized_body_and_postdated_truth_is_proxy() -> None:
    replay = _current_outcome_replay()
    point_in_time = _survivorship(replay)
    with pytest.raises(ValueError, match="normalized body"):
        replace(point_in_time, membership_body=None)

    proxy = _survivorship(
        replay, status=SurvivorshipEvidenceStatus.CURRENT_MEMBERSHIP_PROXY
    )
    corpus = _corpus(status=SurvivorshipEvidenceStatus.CURRENT_MEMBERSHIP_PROXY)
    assert proxy.membership_body is not None
    assert corpus.status is ValidationCorpusStatus.EXTERNAL_DATA_BLOCKED
    assert corpus.limitations == ("survivorship_current_membership_proxy",)


@pytest.mark.parametrize(
    "status",
    (
        SurvivorshipEvidenceStatus.UNKNOWN,
        SurvivorshipEvidenceStatus.UNAVAILABLE,
    ),
)
def test_unknown_survivorship_is_explicit_and_external_data_blocked(status) -> None:
    replay = _current_outcome_replay()
    snapshot = replay.pipeline_result.preparation.universe_snapshot
    evidence = build_validation_survivorship_evidence(
        universe_snapshot=snapshot,
        status=status,
        membership_body=None,
        source_artifacts=(),
        reason="historical membership source unavailable",
    )
    corpus = build_validation_corpus(
        current_replays=(replay,),
        survivorship_evidence=(evidence,),
        policy=_policy(replay),
        frozen_at=replay.outcome_persistence_receipt.persisted_at
        + timedelta(seconds=1),
    )
    assert corpus.status is ValidationCorpusStatus.EXTERNAL_DATA_BLOCKED
    assert corpus.limitations == (f"survivorship_{status.value}",)


def test_validation_corpus_rejects_late_freeze_and_horizon_substitution() -> None:
    corpus = _corpus()
    with pytest.raises(ValueError, match="freezes before"):
        replace(corpus, frozen_at=corpus.rows[0].required_available_at - timedelta(seconds=1))

    payload = corpus.to_dict()
    policy = dict(payload["policy"])
    policy["elapsed_seconds"] = 300
    policy_values = {
        key: value for key, value in policy.items() if key != "policy_id"
    }
    policy["policy_id"] = stable_identity("validation-corpus-policy", policy_values)
    payload["policy"] = policy
    with pytest.raises(ValueError, match="exactly one selected horizon"):
        ValidationCorpus.from_dict(payload)


def test_validation_strict_json_rejects_unknown_and_duplicate_keys() -> None:
    corpus = _corpus()
    with pytest.raises(ValueError, match="unknown field"):
        ValidationCorpus.from_dict({**corpus.to_dict(), "injected": "value"})
    payload = corpus.to_json()
    with pytest.raises(ValueError, match="duplicate JSON key"):
        ValidationCorpus.from_json(payload[:-1] + ',"corpus_id":"duplicate"}')

    nested_float = corpus.to_dict()
    nested_float["sessions"][0]["current_outcome_replays"][0]["outcome_batch"][
        "source_dataset"
    ]["series"][0]["observations"][0]["bar"]["open_price"] = 100.0
    with pytest.raises(ValueError, match="exact Decimal"):
        ValidationCorpus.from_dict(nested_float)


def test_duplicate_replay_and_schema_or_private_lineage_reject() -> None:
    replay = _current_outcome_replay()
    evidence = _survivorship(replay)
    with pytest.raises(ValueError, match="duplicate current replay|duplicate pipeline run"):
        build_validation_corpus(
            current_replays=(replay, replay),
            survivorship_evidence=(evidence,),
            policy=_policy(replay),
            frozen_at=replay.outcome_persistence_receipt.persisted_at
            + timedelta(seconds=1),
        )
    artifact = evidence.source_artifacts[0]
    with pytest.raises(ValueError, match="sanitized"):
        replace(artifact, source_identity="api_key=secret")
    corpus = _corpus()
    with pytest.raises(ValueError, match="unsupported schema_version"):
        ValidationCorpus.from_dict(
            {**corpus.to_dict(), "schema_version": "v2.opportunity.validation_corpus.v99"}
        )


def test_membership_body_exactly_binds_normalized_artifact_lineage() -> None:
    evidence = _survivorship(_current_outcome_replay())
    assert evidence.membership_body is not None
    body = evidence.membership_body
    artifact = body.source_artifacts[0]
    assert type(body).from_json(body.to_json()) == body
    assert type(evidence).from_json(evidence.to_json()) == evidence
    artifact_values = {
        **artifact.__dict__,
        "normalized_artifact_hash_sha256": _hash("unrelated-normalized-body"),
    }
    artifact_values.pop("artifact_id")
    substituted_artifact = replace(
        artifact,
        normalized_artifact_hash_sha256=_hash("unrelated-normalized-body"),
        artifact_id=stable_identity("validation-source-artifact", artifact_values),
    )
    for changes in (
        {"source_identity": "different-membership-source"},
        {"source_version": "fixture-v2"},
        {"method": "different normalized method"},
        {"observed_at": body.observed_at + timedelta(seconds=1)},
        {"normalized_content_hash_sha256": _hash("wrong-normalized-body")},
        {"source_artifacts": (substituted_artifact,)},
    ):
        with pytest.raises(ValueError):
            replace(body, **changes)
    payload = body.to_dict()
    payload["source_identity"] = "different-membership-source"
    body_values = {key: value for key, value in payload.items() if key != "membership_body_id"}
    payload["membership_body_id"] = stable_identity(
        "validation-membership-body", body_values
    )
    with pytest.raises(ValueError, match="normalized membership hash"):
        type(body).from_dict(payload)

    snapshot = evidence.universe_snapshot
    late_artifact = build_validation_source_artifact(
        raw_artifact_hash_sha256=_hash("late-historical-raw"),
        normalized_artifact_hash_sha256=body.normalized_content_hash_sha256,
        provider=artifact.provider,
        source_identity=body.source_identity,
        source_version=body.source_version,
        method=body.method,
        observed_at=body.observed_at,
        fetched_at=body.observed_at + timedelta(days=1),
    )
    late_body = build_validation_membership_body(
        universe_snapshot=snapshot,
        membership_effective_at=body.membership_effective_at,
        observed_at=body.observed_at,
        provider=body.provider,
        source_identity=body.source_identity,
        source_version=body.source_version,
        method=body.method,
        source_artifacts=(late_artifact,),
    )
    assert build_validation_survivorship_evidence(
        universe_snapshot=snapshot,
        status=SurvivorshipEvidenceStatus.POINT_IN_TIME,
        membership_body=late_body,
        source_artifacts=(late_artifact,),
    ).status is SurvivorshipEvidenceStatus.POINT_IN_TIME


def test_incomplete_outcomes_are_retained_without_fake_labels() -> None:
    replay = _stored_replay_for_batch(
        outcome_fixtures._batch(missing_symbol="ABC", missing_index=2)
    )
    evidence = _survivorship(replay)
    corpus = build_validation_corpus(
        current_replays=(replay,),
        survivorship_evidence=(evidence,),
        policy=_policy(replay),
        frozen_at=replay.outcome_persistence_receipt.persisted_at
        + timedelta(seconds=1),
    )
    assert corpus.status is ValidationCorpusStatus.INCOMPLETE
    assert corpus.row_count == len(replay.pipeline_result.preparation.evaluations)
    assert any(item.completeness != "complete" for item in corpus.rows)

    split_policy = build_validation_split_policy(
        policy_version="wp005-a-incomplete-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=0,
        validation_session_count=1,
        locked_oos_session_count=0,
        train_research_required=False,
        locked_oos_required=False,
    )
    preparation = build_chronological_validation_preparation(
        corpus,
        split_policy=split_policy,
        audited_at=corpus.frozen_at + timedelta(seconds=1),
        recorded_at=corpus.frozen_at + timedelta(seconds=2),
    )
    assert preparation.audit_receipt.fold_collection.split_plan.status is (
        SplitPlanStatus.INSUFFICIENT_DATA
    )
    assert preparation.audit_receipt.status is LeakageAuditStatus.INCOMPLETE
    assert preparation.status.value == "insufficient_data"
    assert all(
        item.status is not LeakageCheckStatus.PASSED
        for item in preparation.audit_receipt.checks
        if item.check_id
        in {"region_chronology", "folds_expanding", "fold_validation_disjoint"}
    )


def test_expanding_folds_cover_all_validation_sessions_including_short_last_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _multi_session_corpus(monkeypatch, count=7)
    policy = build_validation_split_policy(
        policy_version="wp005-a-split-v1",
        declared_at=corpus.sessions[0].session_open_at - timedelta(seconds=1),
        train_research_session_count=3,
        validation_session_count=3,
        locked_oos_session_count=1,
        validation_window_session_count=2,
        validation_step_session_count=2,
    )
    plan = build_chronological_split_plan(corpus, policy=policy)
    folds = build_expanding_walk_forward_folds(plan)

    assert plan.status is SplitPlanStatus.AVAILABLE
    assert plan.holdout_integrity_status is (
        HoldoutIntegrityStatus.DECLARED_BEFORE_OOS_NOT_DURABLY_VERIFIED
    )
    assert [item.actual_validation_session_count for item in folds.folds] == [2, 1]
    assert tuple(
        session_id for fold in folds.folds for session_id in fold.validation_session_ids
    ) == folds.validation_session_ids
    assert not set(plan.locked_oos_session_ids).intersection(
        session_id
        for fold in folds.folds
        for session_id in (*fold.train_session_ids, *fold.validation_session_ids)
    )
    assert ChronologicalSplitPlan.from_json(plan.to_json()) == plan
    assert WalkForwardFoldCollection.from_json(folds.to_json()) == folds


def test_equality_purge_and_positional_embargo_are_whole_session_exclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    equality_corpus = _multi_session_corpus(
        monkeypatch, count=3, spacing_seconds=303
    )
    equality_policy = build_validation_split_policy(
        policy_version="wp005-a-equality-v1",
        declared_at=equality_corpus.sessions[0].session_open_at,
        train_research_session_count=1,
        validation_session_count=1,
        locked_oos_session_count=1,
    )
    equality_plan = build_chronological_split_plan(
        equality_corpus, policy=equality_policy
    )
    assert equality_plan.allocations[0].role is SplitRole.PURGED
    assert equality_plan.allocations[1].role is SplitRole.PURGED
    assert equality_plan.status is SplitPlanStatus.INSUFFICIENT_DATA

    safe_corpus = _multi_session_corpus(monkeypatch, count=5)
    embargo_policy = build_validation_split_policy(
        policy_version="wp005-a-embargo-v1",
        declared_at=safe_corpus.sessions[0].session_open_at,
        train_research_session_count=2,
        validation_session_count=2,
        locked_oos_session_count=1,
        region_embargo_session_count=1,
    )
    embargo_plan = build_chronological_split_plan(
        safe_corpus, policy=embargo_policy
    )
    assert [item.role for item in embargo_plan.allocations].count(
        SplitRole.EMBARGOED
    ) == 2


def test_survivorship_fetch_time_drives_exact_boundary_purge_and_audit_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replays = tuple(
        _shifted_replay(monkeypatch, seconds=index * 304, ordinal=index + 1)
        for index in range(2)
    )
    next_decision = replays[1].pipeline_result.decision_at.astimezone(UTC)
    policy = _policy(replays[0])
    split_policy = build_validation_split_policy(
        policy_version="wp005-a-survivorship-purge-v1",
        declared_at=replays[0].outcome_batch.horizons[0].session_open_at,
        train_research_session_count=1,
        validation_session_count=1,
        locked_oos_session_count=0,
        locked_oos_required=False,
    )

    def corpus_at(fetched_at):
        evidence = (
            _survivorship_fetched_at(replays[0], fetched_at),
            _survivorship(replays[1]),
        )
        frozen_at = max(
            *(item.outcome_persistence_receipt.persisted_at for item in replays),
            *(artifact.fetched_at for item in evidence for artifact in item.source_artifacts),
        ) + timedelta(seconds=1)
        return build_validation_corpus(
            current_replays=replays,
            survivorship_evidence=evidence,
            policy=policy,
            frozen_at=frozen_at,
        )

    equality_corpus = corpus_at(next_decision)
    equality_plan = build_chronological_split_plan(
        equality_corpus, policy=split_policy
    )
    assert equality_corpus.rows[0].required_available_at == next_decision
    assert equality_plan.allocations[0].role is SplitRole.PURGED
    equality_folds = build_expanding_walk_forward_folds(equality_plan)
    equality_audit = audit_validation_timestamps(
        equality_folds, audited_at=equality_corpus.frozen_at + timedelta(seconds=1)
    )
    assert equality_audit.maximum_required_available_at == max(
        item.required_available_at for item in equality_corpus.rows
    )
    assert equality_audit.maximum_required_available_at >= next_decision

    retained_corpus = corpus_at(next_decision - timedelta(microseconds=1))
    retained_plan = build_chronological_split_plan(
        retained_corpus, policy=split_policy
    )
    assert retained_corpus.rows[0].required_available_at == (
        next_decision - timedelta(microseconds=1)
    )
    assert retained_plan.allocations[0].role is SplitRole.TRAIN_RESEARCH


def test_required_oos_without_durable_lock_never_passes_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _multi_session_corpus(monkeypatch, count=5)
    policy = build_validation_split_policy(
        policy_version="wp005-a-required-oos-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=2,
        validation_session_count=2,
        locked_oos_session_count=1,
    )
    recorded_at = corpus.frozen_at + timedelta(seconds=2)
    preparation = build_chronological_validation_preparation(
        corpus,
        split_policy=policy,
        audited_at=corpus.frozen_at + timedelta(seconds=1),
        recorded_at=recorded_at,
    )

    assert preparation.audit_receipt.status is LeakageAuditStatus.INCOMPLETE
    holdout = next(
        item
        for item in preparation.audit_receipt.checks
        if item.check_id == "holdout_integrity"
    )
    assert holdout.status.value == "unavailable"
    assert preparation.status.value == "insufficient_data"
    assert not preparation.promotion_eligible
    assert ChronologicalValidationPreparationReceipt.from_json(
        preparation.to_json()
    ) == preparation
    assert TimestampLeakageAuditReceipt.from_json(
        preparation.audit_receipt.to_json()
    ) == preparation.audit_receipt


def test_no_oos_bounded_software_audit_can_pass_without_claiming_a_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _multi_session_corpus(monkeypatch, count=5)
    policy = build_validation_split_policy(
        policy_version="wp005-a-no-oos-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=3,
        validation_session_count=2,
        locked_oos_session_count=0,
        locked_oos_required=False,
    )
    preparation = build_chronological_validation_preparation(
        corpus,
        split_policy=policy,
        audited_at=corpus.frozen_at + timedelta(seconds=1),
        recorded_at=corpus.frozen_at + timedelta(seconds=2),
    )
    assert preparation.audit_receipt.status is LeakageAuditStatus.PASSED_BOUNDED
    assert preparation.audit_receipt.fold_collection.split_plan.holdout_integrity_status is (
        HoldoutIntegrityStatus.UNAVAILABLE
    )
    with pytest.raises(ValueError, match="precedes the frozen corpus"):
        audit_validation_timestamps(
            preparation.audit_receipt.fold_collection,
            audited_at=corpus.frozen_at - timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        build_validation_split_policy(
            policy_version="wp005-a-naive-v1",
            declared_at=corpus.sessions[0].session_open_at.replace(tzinfo=None),
            train_research_session_count=3,
            validation_session_count=2,
            locked_oos_session_count=0,
            locked_oos_required=False,
        )


@pytest.mark.parametrize("embargo_count", (0, 1, 2))
def test_zero_one_and_multiple_embargo_sessions_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    embargo_count: int,
) -> None:
    corpus = _multi_session_corpus(monkeypatch, count=7)
    policy = build_validation_split_policy(
        policy_version=f"wp005-a-embargo-{embargo_count}",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=3,
        validation_session_count=3,
        locked_oos_session_count=1,
        region_embargo_session_count=embargo_count,
    )
    plan = build_chronological_split_plan(corpus, policy=policy)
    assert plan.embargoed_session_count == embargo_count * 2


def test_invalid_fold_window_is_retained_and_marks_collection_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _multi_session_corpus(monkeypatch, count=7)
    policy = build_validation_split_policy(
        policy_version="wp005-a-fold-insufficient-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=3,
        validation_session_count=3,
        locked_oos_session_count=1,
        minimum_fold_training_sessions=99,
        validation_window_session_count=2,
        validation_step_session_count=2,
    )
    folds = build_expanding_walk_forward_folds(
        build_chronological_split_plan(corpus, policy=policy)
    )
    assert [item.actual_validation_session_count for item in folds.folds] == [2, 1]
    assert folds.status is SplitPlanStatus.INSUFFICIENT_DATA
    assert tuple(
        session_id for fold in folds.folds for session_id in fold.validation_session_ids
    ) == folds.validation_session_ids


def test_asymmetric_over_embargo_retains_every_validation_window_as_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _multi_session_corpus(monkeypatch, count=4)
    policy = build_validation_split_policy(
        policy_version="wp005-a-over-embargo-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=1,
        validation_session_count=3,
        locked_oos_session_count=0,
        locked_oos_required=False,
        region_embargo_session_count=1,
        validation_window_session_count=1,
        validation_step_session_count=1,
    )
    plan = build_chronological_split_plan(corpus, policy=policy)
    folds = build_expanding_walk_forward_folds(plan)
    assert plan.status is SplitPlanStatus.INSUFFICIENT_DATA
    assert plan.train_research_session_count == 0
    assert len(folds.validation_session_ids) == 2
    assert tuple(
        session_id for fold in folds.folds for session_id in fold.validation_session_ids
    ) == folds.validation_session_ids
    assert folds.folds[0].status.value == "insufficient_data"
    assert folds.status is SplitPlanStatus.INSUFFICIENT_DATA


def test_retrospective_and_previously_evaluated_holdouts_fail_fresh_lock_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _multi_session_corpus(monkeypatch, count=5)
    first_oos_decision = min(
        item.decision_at
        for item in corpus.rows
        if item.session_source_id == corpus.sessions[-1].session_source_id
    )
    retrospective_policy = build_validation_split_policy(
        policy_version="wp005-a-retrospective-v1",
        declared_at=first_oos_decision.astimezone(UTC),
        train_research_session_count=2,
        validation_session_count=2,
        locked_oos_session_count=1,
    )
    retrospective = build_chronological_split_plan(
        corpus, policy=retrospective_policy
    )
    assert retrospective.holdout_integrity_status is (
        HoldoutIntegrityStatus.RETROSPECTIVE_ONLY
    )

    access = build_holdout_access_evidence(
        status=HoldoutAccessStatus.PREVIOUSLY_EVALUATED,
        observed_at=corpus.frozen_at,
        source_identity="fixture-access-ledger",
        source_version="v1",
        method="immutable historical access event",
        artifact_ids=("access-artifact",),
        artifact_content_hashes=(_hash("access-artifact"),),
    )
    prior_policy = build_validation_split_policy(
        policy_version="wp005-a-prior-use-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=2,
        validation_session_count=2,
        locked_oos_session_count=1,
    )
    prior = build_chronological_split_plan(
        corpus, policy=prior_policy, holdout_access_evidence=access
    )
    assert prior.holdout_integrity_status is (
        HoldoutIntegrityStatus.PREVIOUSLY_EVALUATED
    )
    audit = build_chronological_validation_preparation(
        corpus,
        split_policy=prior_policy,
        holdout_access_evidence=access,
        audited_at=corpus.frozen_at + timedelta(seconds=1),
        recorded_at=corpus.frozen_at + timedelta(seconds=2),
    ).audit_receipt
    assert audit.status is LeakageAuditStatus.FAILED


def test_weak_survivorship_blocks_required_oos_study(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _multi_session_corpus(
        monkeypatch,
        count=5,
        status=SurvivorshipEvidenceStatus.CURRENT_MEMBERSHIP_PROXY,
    )
    policy = build_validation_split_policy(
        policy_version="wp005-a-proxy-block-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=2,
        validation_session_count=2,
        locked_oos_session_count=1,
    )
    preparation = build_chronological_validation_preparation(
        corpus,
        split_policy=policy,
        audited_at=corpus.frozen_at + timedelta(seconds=1),
        recorded_at=corpus.frozen_at + timedelta(seconds=2),
    )
    assert preparation.audit_receipt.status is (
        LeakageAuditStatus.EXTERNAL_DATA_BLOCKED
    )
    assert preparation.status.value == "external_data_blocked"


def test_split_fold_and_audit_consistent_rehash_tamper_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _multi_session_corpus(monkeypatch, count=5)
    policy = build_validation_split_policy(
        policy_version="wp005-a-tamper-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=3,
        validation_session_count=2,
        locked_oos_session_count=0,
        locked_oos_required=False,
    )
    preparation = build_chronological_validation_preparation(
        corpus,
        split_policy=policy,
        audited_at=corpus.frozen_at + timedelta(seconds=1),
        recorded_at=corpus.frozen_at + timedelta(seconds=2),
    )
    plan = preparation.audit_receipt.fold_collection.split_plan
    changed_allocations = (
        replace(plan.allocations[0], role=SplitRole.VALIDATION),
        *plan.allocations[1:],
    )
    plan_values = {**plan.__dict__, "allocations": changed_allocations}
    plan_values.pop("split_plan_id")
    with pytest.raises(ValueError, match="allocations do not recompute"):
        replace(
            plan,
            allocations=changed_allocations,
            split_plan_id=stable_identity("chronological-split-plan", plan_values),
        )
    plan_payload = plan.to_dict()
    plan_payload["allocations"][0]["role"] = "validation"
    plan_payload["split_plan_id"] = stable_identity(
        "chronological-split-plan",
        {key: value for key, value in plan_payload.items() if key != "split_plan_id"},
    )
    with pytest.raises(ValueError, match="allocations do not recompute"):
        ChronologicalSplitPlan.from_dict(plan_payload)

    folds = preparation.audit_receipt.fold_collection
    first_fold = folds.folds[0]
    changed_fold_values = {
        **first_fold.__dict__,
        "train_session_ids": (*first_fold.train_session_ids, "injected-oos"),
    }
    changed_fold_values.pop("fold_id")
    changed_fold = replace(
        first_fold,
        train_session_ids=(*first_fold.train_session_ids, "injected-oos"),
        fold_id=stable_identity("validation-fold", changed_fold_values),
    )
    changed_folds = (changed_fold, *folds.folds[1:])
    collection_values = {**folds.__dict__, "folds": changed_folds}
    collection_values.pop("fold_collection_id")
    with pytest.raises(ValueError, match="folds do not recompute"):
        replace(
            folds,
            folds=changed_folds,
            fold_collection_id=stable_identity(
                "walk-forward-folds", collection_values
            ),
        )

    audit = preparation.audit_receipt
    changed_checks = (
        replace(audit.checks[0], status=LeakageCheckStatus.FAILED),
        *audit.checks[1:],
    )
    audit_values = {**audit.__dict__, "checks": changed_checks}
    audit_values.pop("audit_id")
    with pytest.raises(ValueError, match="checks do not recompute"):
        replace(
            audit,
            checks=changed_checks,
            audit_id=stable_identity("validation-leakage-audit", audit_values),
        )
    audit_payload = audit.to_dict()
    audit_payload["checks"][0]["status"] = "failed"
    audit_payload["audit_id"] = stable_identity(
        "validation-leakage-audit",
        {key: value for key, value in audit_payload.items() if key != "audit_id"},
    )
    with pytest.raises(ValueError, match="checks do not recompute"):
        TimestampLeakageAuditReceipt.from_dict(audit_payload)


def test_future_observation_mutation_changes_only_downstream_validation_identity() -> None:
    original = _current_outcome_replay()
    batch = original.outcome_batch
    first_series = batch.source_dataset.series[0]
    observations = list(first_series.observations)
    observations[-1] = outcome_fixtures._replace_observation_prices(
        observations[-1],
        open_price="100.4",
        high_price="100.8",
        low_price="100.2",
        close_price="100.6",
    )
    changed_series = outcome_fixtures._rebuild_series(
        first_series, observations=tuple(observations)
    )
    changed_batch = outcome_fixtures._relabel_with_series(
        batch, (changed_series, *batch.source_dataset.series[1:])
    )
    changed = _stored_replay_for_batch(changed_batch)
    original_corpus = build_validation_corpus(
        current_replays=(original,),
        survivorship_evidence=(_survivorship(original),),
        policy=_policy(original),
        frozen_at=original.outcome_persistence_receipt.persisted_at
        + timedelta(seconds=1),
    )
    changed_corpus = build_validation_corpus(
        current_replays=(changed,),
        survivorship_evidence=(_survivorship(changed),),
        policy=_policy(changed),
        frozen_at=changed.outcome_persistence_receipt.persisted_at
        + timedelta(seconds=1),
    )
    assert changed_corpus.corpus_id != original_corpus.corpus_id
    assert changed.replay_id != original.replay_id
    assert changed.pipeline_result.run_id == original.pipeline_result.run_id
    assert tuple(item.evaluation_id for item in changed.pipeline_result.evaluations) == tuple(
        item.evaluation_id for item in original.pipeline_result.evaluations
    )
    assert tuple(item.decision_id for item in changed.pipeline_result.decisions) == tuple(
        item.decision_id for item in original.pipeline_result.decisions
    )
    assert tuple(item.trace_id for item in changed.pipeline_result.traces) == tuple(
        item.trace_id for item in original.pipeline_result.traces
    )


def test_validation_import_firewall_and_module_sizes() -> None:
    script = """
import sys
import intraday_scanner.v2.opportunity
import intraday_scanner.v2.opportunity.models
import intraday_scanner.v2.opportunity.features
import intraday_scanner.v2.opportunity.discovery
import intraday_scanner.v2.opportunity.regimes
import intraday_scanner.v2.opportunity.registry
import intraday_scanner.v2.opportunity.ranking
import intraday_scanner.v2.opportunity.risk
import intraday_scanner.v2.opportunity.quality_gate
import intraday_scanner.v2.opportunity.pipeline
import intraday_scanner.storage.opportunity_store
import intraday_scanner.storage.opportunity_outcome_store
import intraday_scanner.storage.opportunity_miss_store
import intraday_scanner.storage.opportunity_metric_store
assert not any(
    name.startswith('intraday_scanner.v2.opportunity.validation')
    for name in sys.modules
)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    forbidden = (
        "alpha.v6",
        "backtest",
        "app",
        "broker",
        "network",
        "scheduler",
        "streamlit",
    )
    files = tuple(
        sorted(
            (Path(__file__).parents[1] / "intraday_scanner/v2/opportunity").glob(
                "validation*.py"
            )
        )
    )
    for path in files:
        assert path.stat().st_size < 40_000
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = tuple(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert not any(token in module for token in forbidden for module in imports)
