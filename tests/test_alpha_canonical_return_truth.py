from __future__ import annotations

import copy
import hashlib
import importlib
import json

import pytest

from tests._alpha_path_truth import (
    canonical_ineligible_outcome,
    canonical_path_receipt,
    canonical_return_outcome,
    canonical_v6_decision,
    causal_identity_from,
    replay_binding_from,
)


def _truth_module():
    return importlib.import_module("intraday_scanner.alpha.canonical_return_truth")


def _valid(payload: dict[str, object], decision: dict[str, object]) -> bool:
    return bool(
        _truth_module().canonical_return_truth_valid(
            payload,
            decision=decision,
        )
    )


def _nested_field_cases() -> tuple[tuple[tuple[str, ...], str], ...]:
    decision = canonical_v6_decision("nested-field-catalog")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    containers = (
        (("path_replay_receipt",), outcome["path_replay_receipt"]),
        (("cost_receipt",), outcome["cost_receipt"]),
        (("cost_receipt", "components"), outcome["cost_components"]),
        (("reconciliation_receipt",), outcome["reconciliation_receipt"]),
        (
            ("reconciliation_receipt", "components"),
            outcome["reconciliation_receipt"]["components"],
        ),
        (("causal_decision_identity",), outcome["causal_decision_identity"]),
        (("replay_binding",), outcome["replay_binding"]),
    )
    return tuple(
        (path, field)
        for path, container in containers
        for field in container.keys()
    )


_ALL_NESTED_FIELD_CASES = _nested_field_cases()


@pytest.mark.parametrize("case", ("ordered_target", "ordered_stop", "timeout"))
def test_current_return_truth_accepts_authentic_resolver_receipts(case: str) -> None:
    decision = canonical_v6_decision(f"current-{case}")
    outcome = canonical_return_outcome(
        case=case,
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )

    assert _valid(outcome, decision)
    assert _truth_module().canonical_return_truth_violations(
        outcome,
        decision=decision,
    ) == ()


@pytest.mark.parametrize("field", tuple(canonical_path_receipt().keys()))
def test_current_return_truth_requires_every_flat_path_projection_field(
    field: str,
) -> None:
    decision = canonical_v6_decision(f"missing-{field}")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    outcome.pop(field)

    assert not _valid(outcome, decision)


@pytest.mark.parametrize(
    "field",
    (
        "path_replay_receipt",
        "replay_binding",
        "outcome_id",
        "outcome_status",
        "activation_status",
        "source_bar_hash_sha256",
        "source_bar_count",
        "exit_event",
        "gross_return_pct",
        "after_cost_return_pct",
        "return_truth_schema_version",
        "return_truth_hash_sha256",
        "cost_schema_version",
        "cost_receipt_id",
        "cost_receipt_hash_sha256",
        "cost_receipt",
        "observed_cost_model_identity",
        "modeled_cost_model_identity",
        "cost_components",
        "benchmark_symbol",
        "benchmark_return_pct",
        "benchmark_source_bar_hash_sha256",
        "benchmark_independent_reconciliation_status",
        "secondary_benchmark_symbol",
        "secondary_benchmark_return_pct",
        "secondary_benchmark_source_bar_hash_sha256",
        "secondary_benchmark_independent_reconciliation_status",
        "net_excess_return_pct",
        "reconciliation_schema_version",
        "independent_reconciliation_status",
        "reconciliation_receipt_id",
        "reconciliation_receipt_hash_sha256",
        "reconciliation_receipt",
        "causal_decision_identity",
        "eligibility_policy_version",
        "learning_eligible",
        "retrospective_research_eligible",
        "prospective_promotion_eligible",
        "evidence_cohort",
        "no_lookahead",
        "validated_against_signal_timestamp",
        "research_only",
        "broker_execution_enabled",
    ),
)
def test_current_return_truth_requires_every_nonpath_contract_dimension(
    field: str,
) -> None:
    decision = canonical_v6_decision(f"missing-{field}")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    outcome.pop(field)

    assert not _valid(outcome, decision)


@pytest.mark.parametrize(
    ("path", "wrong"),
    (
        (("path_replay_schema_version",), "dawnstrike.path_truth.v999"),
        (("path_replay_policy_version",), "attacker-policy"),
        (("path_replay_policy_hash_sha256",), "f" * 64),
        (("path_replay_receipt", "exit_price"), 999.0),
        (("replay_input_manifest", "source_artifact_identity"), "bars:ATTACKER"),
        (("replay_input_hash_sha256",), "f" * 64),
        (("replay_truth_hash_sha256",), "f" * 64),
        (("replay_receipt_hash_sha256",), "f" * 64),
        (("path_replay_id",), "path-v2-" + "f" * 64),
        (("source_artifact_identity",), "bars:ATTACKER"),
        (("source_artifact_hash_sha256",), "f" * 64),
        (("source_bar_hash_sha256",), "f" * 64),
        (("source_bar_count",), 0),
        (("source_bar_count",), "2"),
        (("source_bar_count",), True),
        (("source_bar_count",), 1.5),
        (("source_bar_count",), 999),
        (("source_coverage_complete",), False),
        (("source_conflict",), True),
        (("corporate_action_unresolved",), True),
        (("sequence_complete_through_exit",), False),
        (("path_truth_status",), "RESOLVED_STOP_FIRST"),
        (("path_event",), "STOP"),
        (("entry_time",), "2026-08-03T14:31:00+00:00"),
        (("entry_price",), 99.0),
        (("exit_time",), "2026-08-03T14:31:30+00:00"),
        (("exit_price",), 999.0),
        (("exit_event",), "STOP"),
        (("outcome_id",), "outcome-v2-" + "f" * 64),
        (("outcome_status",), "COMPLETE_SOURCED"),
        (("activation_status",), "NOT_ACTIVATED"),
        (("return_truth_schema_version",), "attacker-return-v9"),
        (("return_truth_hash_sha256",), "f" * 64),
        (("cost_schema_version",), "attacker-cost-v9"),
        (("cost_receipt_id",), "attacker-cost"),
        (("cost_receipt_hash_sha256",), "f" * 64),
        (("cost_receipt", "receipt_id"), "attacker-cost"),
        (("cost_receipt", "attacker_extra"), True),
        (("cost_receipt", "raw_entry_price"), 99.0),
        (("cost_receipt", "after_cost_return_pct"), 99.0),
        (("observed_cost_model_identity",), "attacker-model"),
        (("modeled_cost_model_identity",), "attacker-model"),
        (("cost_components", "notional_per_trade"), 0.0),
        (("cost_components", "entry_slippage_bps"), -1.0),
        (("cost_components", "exit_slippage_bps"), "50"),
        (("cost_components", "fee_bps_per_side"), -1.0),
        (("cost_components", "commission_per_share_per_side"), -1.0),
        (("gross_return_pct",), 99.0),
        (("after_cost_return_pct",), 99.0),
        (("benchmark_symbol",), "QQQ"),
        (("benchmark_return_pct",), 99.0),
        (("benchmark_source_bar_hash_sha256",), "f" * 64),
        (("benchmark_independent_reconciliation_status",), "FAILED"),
        (("secondary_benchmark_symbol",), "QQQ"),
        (("secondary_benchmark_return_pct",), 99.0),
        (("secondary_benchmark_source_bar_hash_sha256",), "f" * 64),
        (("secondary_benchmark_independent_reconciliation_status",), "PENDING"),
        (("net_excess_return_pct",), 99.0),
        (("reconciliation_schema_version",), "attacker-recon-v9"),
        (("reconciliation_receipt_id",), "attacker-recon"),
        (("reconciliation_receipt_hash_sha256",), "f" * 64),
        (("reconciliation_receipt", "receipt_id"), "attacker-recon"),
        (("reconciliation_receipt", "attacker_extra"), True),
        (("reconciliation_receipt", "status"), "FAILED"),
        (("reconciliation_receipt", "components", "path_replay_id"), "attacker"),
        (
            ("reconciliation_receipt", "components", "cost_receipt_hash_sha256"),
            "f" * 64,
        ),
        (
            (
                "reconciliation_receipt",
                "components",
                "primary_benchmark_return_pct",
            ),
            99.0,
        ),
        (("independent_reconciliation_status",), "FAILED"),
        (("causal_decision_identity", "kind"), "fabricated"),
        (("causal_decision_identity", "attacker_extra"), True),
        (("causal_decision_identity", "decision_id"), "attacker-decision"),
        (("causal_decision_identity", "decision_at"), "2026-08-03T23:59:00+00:00"),
        (("causal_decision_identity", "input_hash_sha256"), "f" * 64),
        (("causal_decision_identity", "source_lineage_hash_sha256"), "f" * 64),
        (("causal_decision_identity", "decision_context_hash_sha256"), "f" * 64),
        (("eligibility_policy_version",), "attacker-eligibility-v9"),
        (("learning_eligible",), "true"),
        (("retrospective_research_eligible",), "true"),
        (("prospective_promotion_eligible",), "true"),
        (("evidence_cohort",), "attacker-cohort"),
        (("no_lookahead",), False),
        (("validated_against_signal_timestamp",), False),
        (("research_only",), False),
        (("broker_execution_enabled",), True),
    ),
)
def test_current_return_truth_rejects_every_wrong_nonblank_dimension(
    path: tuple[str, ...],
    wrong: object,
) -> None:
    decision = canonical_v6_decision("mutation-decision")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )

    assert not _valid(_mutate(outcome, path, wrong), decision)


def test_prospective_truth_requires_retrospective_truth() -> None:
    decision = canonical_v6_decision("prospective-without-retro")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    outcome["retrospective_research_eligible"] = False
    outcome["prospective_promotion_eligible"] = True

    assert not _valid(outcome, decision)


@pytest.mark.parametrize(
    "path",
    (
        ("path_replay_receipt",),
        ("outcome_id",),
        ("outcome_status",),
        ("activation_status",),
        ("return_truth_schema_version",),
        ("return_truth_hash_sha256",),
        ("source_bar_hash_sha256",),
        ("source_bar_count",),
        ("exit_event",),
        ("gross_return_pct",),
        ("after_cost_return_pct",),
        ("cost_schema_version",),
        ("cost_receipt_id",),
        ("cost_receipt_hash_sha256",),
        ("observed_cost_model_identity",),
        ("modeled_cost_model_identity",),
        ("benchmark_symbol",),
        ("benchmark_return_pct",),
        ("benchmark_source_bar_hash_sha256",),
        ("benchmark_independent_reconciliation_status",),
        ("secondary_benchmark_symbol",),
        ("secondary_benchmark_return_pct",),
        ("secondary_benchmark_source_bar_hash_sha256",),
        ("secondary_benchmark_independent_reconciliation_status",),
        ("reconciliation_schema_version",),
        ("independent_reconciliation_status",),
        ("reconciliation_receipt_id",),
        ("reconciliation_receipt_hash_sha256",),
        ("net_excess_return_pct",),
        ("eligibility_policy_version",),
        ("evidence_cohort",),
        ("learning_eligible",),
        ("retrospective_research_eligible",),
        ("prospective_promotion_eligible",),
        ("no_lookahead",),
        ("validated_against_signal_timestamp",),
        ("research_only",),
        ("broker_execution_enabled",),
        ("cost_receipt", "schema_version"),
        ("cost_receipt", "path_replay_id"),
        ("cost_receipt", "receipt_id"),
        ("cost_receipt", "receipt_hash_sha256"),
        ("cost_receipt", "observed_cost_model_identity"),
        ("cost_receipt", "modeled_cost_model_identity"),
        ("cost_receipt", "raw_entry_price"),
        ("cost_receipt", "raw_exit_price"),
        ("cost_receipt", "gross_return_pct"),
        ("cost_receipt", "after_cost_return_pct"),
        ("cost_receipt", "components", "notional_per_trade"),
        ("cost_receipt", "components", "entry_slippage_bps"),
        ("cost_receipt", "components", "exit_slippage_bps"),
        ("cost_receipt", "components", "fee_bps_per_side"),
        ("cost_receipt", "components", "commission_per_share_per_side"),
        ("reconciliation_receipt", "schema_version"),
        ("reconciliation_receipt", "receipt_id"),
        ("reconciliation_receipt", "receipt_hash_sha256"),
        ("reconciliation_receipt", "status"),
        (
            "reconciliation_receipt",
            "components",
            "primary_benchmark_symbol",
        ),
        (
            "reconciliation_receipt",
            "components",
            "primary_benchmark_return_pct",
        ),
        (
            "reconciliation_receipt",
            "components",
            "secondary_benchmark_symbol",
        ),
        (
            "reconciliation_receipt",
            "components",
            "secondary_benchmark_return_pct",
        ),
        (
            "reconciliation_receipt",
            "components",
            "after_cost_return_pct",
        ),
        (
            "reconciliation_receipt",
            "components",
            "net_excess_return_pct",
        ),
        ("causal_decision_identity", "kind"),
        ("causal_decision_identity", "decision_id"),
        ("causal_decision_identity", "decision_at"),
        ("causal_decision_identity", "input_hash_sha256"),
        ("causal_decision_identity", "source_lineage_hash_sha256"),
        ("causal_decision_identity", "decision_context_hash_sha256"),
    ),
)
def test_current_return_truth_rejects_blank_required_values(
    path: tuple[str, ...],
) -> None:
    decision = canonical_v6_decision("blank-contract")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )

    assert not _valid(_mutate(outcome, path, ""), decision)


@pytest.mark.parametrize("field", tuple(canonical_path_receipt().keys()))
def test_current_return_truth_rejects_blank_flat_path_fields(field: str) -> None:
    decision = canonical_v6_decision(f"blank-path-{field}")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )

    assert not _valid(_mutate(outcome, (field,), ""), decision)


@pytest.mark.parametrize(
    ("container_path", "field"),
    tuple(
        (("path_replay_receipt",), field)
        for field in canonical_path_receipt().keys()
    )
    + tuple(
        (("cost_receipt",), field)
        for field in canonical_return_outcome(
            causal_identity=causal_identity_from(
                canonical_v6_decision("cost-key-fixture"),
                kind="alpha_v6_shadow_decision",
            )
        )["cost_receipt"].keys()
    )
    + tuple(
        (("cost_receipt", "components"), field)
        for field in canonical_return_outcome(
            causal_identity=causal_identity_from(
                canonical_v6_decision("cost-component-fixture"),
                kind="alpha_v6_shadow_decision",
            )
        )["cost_components"].keys()
    )
    + tuple(
        (("reconciliation_receipt",), field)
        for field in canonical_return_outcome(
            causal_identity=causal_identity_from(
                canonical_v6_decision("reconciliation-key-fixture"),
                kind="alpha_v6_shadow_decision",
            )
        )["reconciliation_receipt"].keys()
    )
    + tuple(
        (("reconciliation_receipt", "components"), field)
        for field in canonical_return_outcome(
            causal_identity=causal_identity_from(
                canonical_v6_decision("reconciliation-component-fixture"),
                kind="alpha_v6_shadow_decision",
            )
        )["reconciliation_receipt"]["components"].keys()
    )
    + tuple(
        (("causal_decision_identity",), field)
        for field in causal_identity_from(
            canonical_v6_decision("causal-key-fixture"),
            kind="alpha_v6_shadow_decision",
        ).keys()
    ),
)
def test_current_return_truth_requires_exact_nested_key_sets(
    container_path: tuple[str, ...],
    field: str,
) -> None:
    decision = canonical_v6_decision("nested-key-contract")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    mutated = copy.deepcopy(outcome)
    cursor: dict[str, object] = mutated
    for key in container_path:
        child = cursor[key]
        assert isinstance(child, dict)
        cursor = child
    cursor.pop(field)

    assert not _valid(mutated, decision)


@pytest.mark.parametrize(
    "container_path",
    (
        ("path_replay_receipt",),
        ("cost_receipt",),
        ("cost_receipt", "components"),
        ("reconciliation_receipt",),
        ("reconciliation_receipt", "components"),
        ("causal_decision_identity",),
    ),
)
def test_current_return_truth_rejects_extra_nested_keys(
    container_path: tuple[str, ...],
) -> None:
    decision = canonical_v6_decision("nested-extra-contract")
    mutated = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    cursor: dict[str, object] = mutated
    for key in container_path:
        child = cursor[key]
        assert isinstance(child, dict)
        cursor = child
    cursor["attacker_extra"] = True

    assert not _valid(mutated, decision)


@pytest.mark.parametrize(("container_path", "field"), _ALL_NESTED_FIELD_CASES)
def test_current_return_truth_rejects_every_nested_field_mutation(
    container_path: tuple[str, ...],
    field: str,
) -> None:
    decision = canonical_v6_decision("nested-field-contract")
    mutated = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    cursor: dict[str, object] = mutated
    for key in container_path:
        child = cursor[key]
        assert isinstance(child, dict)
        cursor = child
    cursor[field] = _different_value(cursor[field])

    assert not _valid(mutated, decision)


@pytest.mark.parametrize(
    ("path", "changed"),
    (
        (("decision_id",), "decision-attacker"),
        (("scan_id",), "scan-attacker"),
        (("source_signal_id",), "source-attacker"),
        (("shadow_signal_id",), "shadow-attacker"),
        (("market_date",), "2026-08-04"),
        (("decision_at",), "2026-08-03T12:11:00+00:00"),
        (("ticker",), "ATTK"),
        (("strategy_version",), "attacker-strategy"),
        (("model_version",), "attacker-model"),
        (("feature_schema_version",), "attacker-feature-schema"),
        (("feature_hash_sha256",), "e" * 64),
        (("input_hash_sha256",), "e" * 64),
        (("source_lineage_hash_sha256",), "e" * 64),
        (("action",), "SHADOW_REJECTED_POLICY"),
        (("decision_state",), "BLOCKED"),
        (("setup_key",), "attacker-setup"),
        (("regime_key",), "attacker-regime"),
        (("point_in_time",), {"all_inputs_observed_at_or_before_decision": False}),
        (("source_summary",), {"status": "conflicted"}),
        (("experiment_assignment", "arm"), "candidate"),
        (("experiment_assignment", "experiment_id"), "experiment-attacker"),
        (
            ("experiment_assignment", "configuration_hash_sha256"),
            "e" * 64,
        ),
        (("signal_facts", "entry_watch_level"), 999.0),
        (("signal_facts", "target_1"), 999.0),
        (("signal_facts", "invalidation_level"), 999.0),
        (("cost_model_version",), "attacker-cost-model"),
        (("estimated_round_trip_cost_bps",), 999.0),
        (("safety_vetoes",), ["source_conflict"]),
        (("evidence_cohort",), "attacker-cohort"),
        (("research_only",), False),
        (("broker_execution_enabled",), True),
    ),
)
def test_causal_decision_context_mutations_invalidate_bound_truth(
    path: tuple[str, ...],
    changed: object,
) -> None:
    decision = {
        **canonical_v6_decision("context-bound"),
        "experiment_assignment": {
            "experiment_id": "experiment-v2",
            "arm": "baseline",
            "configuration_hash_sha256": "d" * 64,
        },
    }
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    mutated_decision = _mutate(decision, path, changed)

    assert not _valid(outcome, mutated_decision)
    assert causal_identity_from(
        decision,
        kind="alpha_v6_shadow_decision",
    )["decision_context_hash_sha256"] != causal_identity_from(
        mutated_decision,
        kind="alpha_v6_shadow_decision",
    )["decision_context_hash_sha256"]


@pytest.mark.parametrize(
    "field",
    (
        "selection_id",
        "scan_id",
        "signal_id",
        "ticker",
        "market_date",
        "strategy_id",
        "strategy_version",
        "cohort",
        "selected_at",
        "input_hash_sha256",
        "source_lineage_hash_sha256",
        "delivery_identity",
        "source_artifact_identity",
        "source_artifact_hash_sha256",
        "research_only",
        "broker_execution_enabled",
    ),
)
def test_paper_causal_context_mutations_invalidate_bound_truth(field: str) -> None:
    selection = {
        "selection_id": "selection-current",
        "scan_id": "scan-current",
        "signal_id": "signal-current",
        "ticker": "NOVA",
        "market_date": "2026-08-03",
        "strategy_id": "alphaops_v5",
        "strategy_version": "dawnstrike-alphaops-v5.0.0",
        "cohort": "official_telegram",
        "decision": "clean_edge",
        "selected_at": "2026-08-03T13:10:00+00:00",
        "input_hash_sha256": "8" * 64,
        "source_lineage_hash_sha256": "9" * 64,
        "delivery_identity": {
            "channel": "telegram",
            "delivery_status": "delivered",
        },
        "source_artifact_identity": "alpha-selection:selection-current",
        "source_artifact_hash_sha256": "4" * 64,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            selection,
            kind="alpha_paper_selection",
            id_key="selection_id",
            time_key="selected_at",
        )
    )
    mutated_selection = copy.deepcopy(selection)
    mutated_selection[field] = _different_value(mutated_selection[field])

    assert not _valid(outcome, mutated_selection)


@pytest.mark.parametrize(
    ("path", "unsafe"),
    (
        (("research_only",), False),
        (("broker_execution_enabled",), True),
        (("source_summary",), {"status": "conflicted"}),
        (("source_summary",), {"status": "incomplete"}),
        (("point_in_time",), {"all_inputs_observed_at_or_before_decision": False}),
        (("safety_vetoes",), ["source_conflict"]),
        (("evidence_cohort",), "legacy-forward-v1"),
        (("action",), "SHADOW_REJECT_VETO"),
        (("decision_state",), "BLOCKED"),
        (
            ("experiment_assignment",),
            {
                "experiment_id": "experiment-v2",
                "arm": "attacker",
                "configuration_hash_sha256": "d" * 64,
            },
        ),
        (
            ("experiment_assignment",),
            {
                "experiment_id": "",
                "arm": "baseline",
                "configuration_hash_sha256": "not-a-hash",
            },
        ),
    ),
)
def test_fully_rehashed_v6_truth_rejects_unsafe_decision_context(
    path: tuple[str, ...],
    unsafe: object,
) -> None:
    decision = {
        **canonical_v6_decision("unsafe-v6-context"),
        "experiment_assignment": {
            "experiment_id": "experiment-v2",
            "arm": "baseline",
            "configuration_hash_sha256": "d" * 64,
        },
    }
    unsafe_decision = _mutate(decision, path, unsafe)
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            unsafe_decision,
            kind="alpha_v6_shadow_decision",
        )
    )

    assert not _valid(outcome, unsafe_decision)


@pytest.mark.parametrize(
    ("field", "unsafe"),
    (
        ("research_only", False),
        ("broker_execution_enabled", True),
        ("cohort", "legacy-paper-v1"),
        ("source_artifact_identity", ""),
        (
            "delivery_identity",
            {"channel": "telegram", "delivery_status": "failed"},
        ),
        (
            "delivery_identity",
            {"channel": "email", "delivery_status": "delivered"},
        ),
    ),
)
def test_fully_rehashed_paper_truth_rejects_unsafe_selection_context(
    field: str,
    unsafe: object,
) -> None:
    selection = _canonical_paper_selection()
    selection[field] = unsafe
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            selection,
            kind="alpha_paper_selection",
            id_key="selection_id",
            time_key="selected_at",
        )
    )

    assert not _valid(outcome, selection)


def test_pre_v5_paper_selection_cannot_claim_current_return_truth() -> None:
    selection = {
        **_canonical_paper_selection(),
        "market_date": "2026-07-13",
        "selected_at": "2026-07-13T13:10:00+00:00",
        "strategy_id": "alphaops_v4",
        "strategy_version": "dawnstrike-alphaops-v4",
    }
    outcome = canonical_return_outcome(
        market_date="2026-07-13",
        causal_identity=causal_identity_from(
            selection,
            kind="alpha_paper_selection",
            id_key="selection_id",
            time_key="selected_at",
        ),
    )

    assert not _valid(outcome, selection)


def test_non_clean_edge_paper_selection_cannot_claim_current_return_truth() -> None:
    selection = {
        **_canonical_paper_selection(),
        "decision": "probability_fallback",
    }
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            selection,
            kind="alpha_paper_selection",
            id_key="selection_id",
            time_key="selected_at",
        ),
    )

    assert not _valid(outcome, selection)


def test_source_bar_count_must_equal_decision_bounded_replay_rows() -> None:
    decision = canonical_v6_decision("source-count-bound")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    outcome["source_bar_count"] = 999
    _rehash_return_and_outcome(outcome)

    assert not _valid(outcome, decision)


@pytest.mark.parametrize(
    "field",
    (
        "scan_id",
        "source_signal_id",
        "shadow_signal_id",
        "market_date",
        "ticker",
        "strategy_version",
        "model_version",
        "feature_schema_version",
        "feature_hash_sha256",
        "setup_key",
        "regime_key",
        "signal_facts",
        "cost_model_version",
        "estimated_round_trip_cost_bps",
    ),
)
def test_fully_rehashed_v6_truth_rejects_missing_required_context(
    field: str,
) -> None:
    decision = canonical_v6_decision(f"missing-v6-{field}")
    decision.pop(field)
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )

    assert not _valid(outcome, decision)


@pytest.mark.parametrize(
    "field",
    ("scan_id", "signal_id", "ticker", "market_date", "strategy_id", "strategy_version"),
)
def test_fully_rehashed_paper_truth_rejects_missing_required_context(
    field: str,
) -> None:
    selection = _canonical_paper_selection()
    selection.pop(field)
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            selection,
            kind="alpha_paper_selection",
            id_key="selection_id",
            time_key="selected_at",
        )
    )

    assert not _valid(outcome, selection)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("scan_id", ""),
        ("source_signal_id", " "),
        ("shadow_signal_id", 1),
        ("market_date", "20260803"),
        ("ticker", ""),
        ("strategy_version", None),
        ("model_version", " "),
        ("feature_schema_version", 1),
        ("feature_hash_sha256", "not-a-hash"),
        ("setup_key", ""),
        ("regime_key", 1),
        ("signal_facts", None),
        ("cost_model_version", ""),
        ("estimated_round_trip_cost_bps", -1.0),
        ("estimated_round_trip_cost_bps", True),
    ),
)
def test_fully_rehashed_v6_truth_rejects_malformed_required_context(
    field: str,
    invalid: object,
) -> None:
    decision = canonical_v6_decision(f"malformed-v6-{field}")
    decision[field] = invalid
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )

    assert not _valid(outcome, decision)


def test_v6_nonfinite_cost_estimate_is_rejected_without_rehashing_it() -> None:
    decision = canonical_v6_decision("nonfinite-cost")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    decision["estimated_round_trip_cost_bps"] = float("nan")

    assert not _valid(outcome, decision)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("scan_id", ""),
        ("signal_id", " "),
        ("ticker", 1),
        ("market_date", "2026-W32-1"),
        ("strategy_id", None),
        ("strategy_version", ""),
        ("source_artifact_identity", " "),
        ("source_artifact_hash_sha256", "not-a-hash"),
    ),
)
def test_fully_rehashed_paper_truth_rejects_malformed_required_context(
    field: str,
    invalid: object,
) -> None:
    selection = _canonical_paper_selection()
    selection[field] = invalid
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            selection,
            kind="alpha_paper_selection",
            id_key="selection_id",
            time_key="selected_at",
        )
    )

    assert not _valid(outcome, selection)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("source_artifact_identity", ""),
        ("source_artifact_hash_sha256", "not-a-hash"),
    ),
)
def test_v6_source_summary_requires_explicit_causal_source_context(
    field: str,
    invalid: object,
) -> None:
    decision = canonical_v6_decision(f"v6-source-context-{field}")
    source_summary = dict(decision["source_summary"])
    source_summary[field] = invalid
    decision["source_summary"] = source_summary
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )

    assert not _valid(outcome, decision)


@pytest.mark.parametrize(
    "path",
    (
        ("subject", "symbol"),
        ("subject", "market_date"),
        ("origin", "id"),
        ("origin", "context_hash_sha256"),
        ("origin", "lineage", "decision_id"),
        ("origin", "lineage", "scan_id"),
        ("origin", "lineage", "source_signal_id"),
        ("origin", "lineage", "shadow_signal_id"),
    ),
)
def test_hash_authenticated_replay_binding_rejects_decision_mismatch(
    path: tuple[str, ...],
) -> None:
    decision = canonical_v6_decision(f"binding-mismatch-{'-'.join(path)}")
    binding = replay_binding_from(decision, kind="alpha_v6_shadow_decision")
    changed = (
        "f" * 64
        if path[-1] == "context_hash_sha256"
        else "ATTK"
        if path == ("subject", "symbol")
        else "2026-08-02"
        if path == ("subject", "market_date")
        else "attacker"
    )
    mutated_binding = _mutate(binding, path, changed)
    if path == ("origin", "id"):
        mutated_binding = _mutate(
            mutated_binding,
            ("origin", "lineage", "decision_id"),
            changed,
        )
    elif path == ("origin", "lineage", "decision_id"):
        mutated_binding = _mutate(
            mutated_binding,
            ("origin", "id"),
            changed,
        )
    if path[:1] == ("subject",):
        with pytest.raises(ValueError, match="invalid canonical replay input manifest"):
            canonical_return_outcome(
                causal_identity=causal_identity_from(
                    decision,
                    kind="alpha_v6_shadow_decision",
                ),
                replay_binding=mutated_binding,
            )
        return
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
        replay_binding=mutated_binding,
    )

    assert not _valid(outcome, decision)


def test_nova_bound_receipt_cannot_validate_for_attacker_symbol_or_date() -> None:
    nova = canonical_v6_decision("nova-origin")
    attacker = canonical_v6_decision("attacker-origin", market_date="2026-08-02")
    attacker["ticker"] = "ATTK"
    outcome = canonical_return_outcome(
        market_date="2026-08-03",
        symbol="NOVA",
        causal_identity=causal_identity_from(
            attacker,
            kind="alpha_v6_shadow_decision",
        ),
        replay_binding=replay_binding_from(
            nova,
            kind="alpha_v6_shadow_decision",
        ),
    )

    assert not _valid(outcome, attacker)


@pytest.mark.parametrize(
    ("ticker", "market_date"),
    (("ATTK", "2026-08-03"), ("NOVA", "2026-08-02")),
)
def test_co_mutated_decision_and_binding_cannot_relabel_future_evidence_subject(
    ticker: str,
    market_date: str,
) -> None:
    decision = canonical_v6_decision("co-mutated-subject", market_date=market_date)
    decision["ticker"] = ticker
    binding = replay_binding_from(decision, kind="alpha_v6_shadow_decision")

    with pytest.raises((AssertionError, ValueError)):
        canonical_return_outcome(
            market_date="2026-08-03",
            symbol="NOVA",
            causal_identity=causal_identity_from(
                decision,
                kind="alpha_v6_shadow_decision",
            ),
            replay_binding=binding,
        )


def test_same_symbol_and_date_cannot_reuse_a_different_origin_receipt() -> None:
    first = canonical_v6_decision("first-origin")
    second = canonical_v6_decision("second-origin")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            second,
            kind="alpha_v6_shadow_decision",
        ),
        replay_binding=replay_binding_from(
            first,
            kind="alpha_v6_shadow_decision",
        ),
    )

    assert not _valid(outcome, second)


@pytest.mark.parametrize("field", ("selection_id", "scan_id", "signal_id"))
def test_paper_replay_binding_rejects_every_lineage_id_mismatch(field: str) -> None:
    selection = _canonical_paper_selection()
    binding = replay_binding_from(
        selection,
        kind="alpha_paper_selection",
        id_key="selection_id",
    )
    mutated_binding = _mutate(
        binding,
        ("origin", "lineage", field),
        "attacker",
    )
    if field == "selection_id":
        mutated_binding = _mutate(
            mutated_binding,
            ("origin", "id"),
            "attacker",
        )
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            selection,
            kind="alpha_paper_selection",
            id_key="selection_id",
            time_key="selected_at",
        ),
        replay_binding=mutated_binding,
    )

    assert not _valid(outcome, selection)


def test_payload_only_replay_binding_cannot_override_path_authenticated_binding() -> None:
    decision = canonical_v6_decision("payload-binding-override")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
        replay_binding=replay_binding_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )
    forged = _mutate(outcome["replay_binding"], ("subject", "symbol"), "ATTK")
    outcome["replay_binding"] = forged
    _rehash_return_and_outcome(outcome)

    assert not _valid(outcome, decision)


def test_nonreturn_outcome_identity_binds_authenticated_replay_origin() -> None:
    decision = canonical_v6_decision("nonreturn-replay-binding")
    outcome = canonical_ineligible_outcome(
        case="not_triggered",
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
        replay_binding=replay_binding_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )
    forged = _mutate(outcome["replay_binding"], ("origin", "id"), "attacker")
    outcome["replay_binding"] = forged
    identity = {
        "schema_version": "dawnstrike.alphaops.return_truth.v2",
        "path_replay_id": outcome["path_replay_id"],
        "path_replay_receipt_hash_sha256": outcome[
            "replay_receipt_hash_sha256"
        ],
        "causal_decision_identity": outcome["causal_decision_identity"],
        "replay_binding": forged,
    }
    outcome["outcome_id"] = "outcome-v2-" + hashlib.sha256(
        json.dumps(
            identity,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()

    assert (
        _truth_module().classify_canonical_return_truth(outcome, decision=decision)
        == "LEGACY_OR_INCOMPLETE"
    )


def test_future_path_artifact_remains_distinct_from_decision_source_context() -> None:
    decision = canonical_v6_decision("distinct-future-source")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
        replay_binding=replay_binding_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )
    source_summary = decision["source_summary"]
    assert isinstance(source_summary, dict)

    assert source_summary["source_artifact_identity"] != outcome[
        "source_artifact_identity"
    ]
    assert source_summary["source_artifact_hash_sha256"] != outcome[
        "source_artifact_hash_sha256"
    ]
    assert _valid(outcome, decision)


def test_outcome_identity_binds_full_return_truth_and_causal_decision() -> None:
    decision = canonical_v6_decision("outcome-identity")
    baseline = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    retro_only = canonical_return_outcome(
        prospective=False,
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )
    identity = {
        "schema_version": baseline["return_truth_schema_version"],
        "return_truth_hash_sha256": baseline["return_truth_hash_sha256"],
        "causal_decision_identity": baseline["causal_decision_identity"],
        "replay_binding": baseline["replay_binding"],
    }
    identity_hash = hashlib.sha256(
        json.dumps(
            identity,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()

    assert baseline["outcome_id"] == f"outcome-v2-{identity_hash}"
    assert baseline["outcome_id"] != retro_only["outcome_id"]
    assert baseline["return_truth_hash_sha256"] != retro_only[
        "return_truth_hash_sha256"
    ]


@pytest.mark.parametrize(
    "case",
    (
        "not_triggered",
        "entry_censored",
        "same_censored",
        "missing_interval",
        "halt",
        "source_conflict",
        "corporate_action",
    ),
)
def test_nonreturn_path_receipts_never_validate_as_return_truth(case: str) -> None:
    decision = canonical_v6_decision(f"ineligible-{case}")
    outcome = canonical_ineligible_outcome(
        case=case,
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )

    assert not _valid(outcome, decision)


def test_legacy_alias_poison_never_overrides_current_canonical_truth() -> None:
    decision = canonical_v6_decision("poison-aliases")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    poisoned = {
        **outcome,
        "net_return_pct": 999.0,
        "planned_first_touch_outcome": "invalidation",
        "target_touched_at_legacy": "2099-01-01T00:00:00+00:00",
        "high_after_entry": 9_999.0,
        "low_after_entry": 0.01,
        "decision_cost_bps": -1_000_000.0,
    }

    assert _valid(poisoned, decision)
    projection = _truth_module().canonical_return_truth_projection(
        poisoned,
        decision=decision,
    )
    assert projection["after_cost_return_pct"] == outcome["after_cost_return_pct"]
    assert projection["path_event"] == outcome["path_event"]
    assert projection["mfe_price"] == outcome["mfe_price"]
    assert projection["mae_price"] == outcome["mae_price"]
    assert projection["outcome_id"] == outcome["outcome_id"]
    assert projection["outcome_status"] == "complete_sourced"
    assert projection["activation_status"] == "ACTIVATED"
    assert "net_return_pct" not in projection
    assert "planned_first_touch_outcome" not in projection
    projection["path_replay_receipt"]["exit_price"] = 999.0
    assert outcome["path_replay_receipt"]["exit_price"] != 999.0


def test_return_projection_ignores_unvalidated_activation_poison() -> None:
    decision = canonical_v6_decision("return-projection-poison")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    outcome["activation_label_eligible"] = _DeepcopyBomb()

    projection = _truth_module().canonical_return_truth_projection(
        outcome,
        decision=decision,
    )

    assert "activation_label_eligible" not in projection
    assert projection["learning_eligible"] is True


@pytest.mark.parametrize(
    "field",
    (
        "mfe_pct",
        "mae_pct",
        "max_favorable_excursion_pct",
        "max_adverse_excursion_pct",
    ),
)
def test_activation_only_projection_ignores_unvalidated_excursion_poison(
    field: str,
) -> None:
    decision = canonical_v6_decision(f"activation-projection-{field}")
    outcome = canonical_ineligible_outcome(
        case="not_triggered",
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )
    outcome[field] = _DeepcopyBomb()

    projection = _truth_module().canonical_return_truth_projection(
        outcome,
        decision=decision,
    )

    assert field not in projection
    assert projection["activation_label_eligible"] is True


@pytest.mark.parametrize(
    ("payload_kind", "expected"),
    (
        ("return", "CURRENT_RETURN_TRUTH"),
        ("not_triggered", "CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED"),
        ("censored", "CURRENT_CENSORED_PATH"),
        ("legacy", "LEGACY_OR_INCOMPLETE"),
        ("terminal", "TERMINAL_MISSING"),
    ),
)
def test_return_truth_classification_is_explicit(
    payload_kind: str,
    expected: str,
) -> None:
    decision = canonical_v6_decision(f"classification-{payload_kind}")
    causal = causal_identity_from(
        decision,
        kind="alpha_v6_shadow_decision",
    )
    if payload_kind == "return":
        payload = canonical_return_outcome(causal_identity=causal)
    elif payload_kind == "not_triggered":
        payload = canonical_ineligible_outcome(
            case="not_triggered",
            causal_identity=causal,
        )
    elif payload_kind == "censored":
        payload = canonical_ineligible_outcome(
            case="same_censored",
            causal_identity=causal,
        )
    elif payload_kind == "terminal":
        payload = {"outcome_status": "TERMINAL_MISSING"}
    else:
        payload = {
            "outcome_status": "COMPLETE_SOURCED",
            "learning_eligible": True,
            "net_return_pct": 99.0,
        }

    assert (
        _truth_module().classify_canonical_return_truth(payload, decision=decision)
        == expected
    )
    if payload_kind == "not_triggered":
        projection = _truth_module().canonical_return_truth_projection(
            payload,
            decision=decision,
        )
        assert projection["activation_label_eligible"] is True
        assert projection["learning_eligible"] is True
        assert projection["retrospective_research_eligible"] is False
        assert projection["prospective_promotion_eligible"] is False
    elif payload_kind == "censored":
        projection = _truth_module().canonical_return_truth_projection(
            payload,
            decision=decision,
        )
        assert projection["activation_label_eligible"] is False
        assert projection["learning_eligible"] is False
        assert projection["retrospective_research_eligible"] is False
        assert projection["prospective_promotion_eligible"] is False


@pytest.mark.parametrize("claim", ("path", "return"))
def test_terminal_missing_cannot_collide_with_current_truth_claims(
    claim: str,
) -> None:
    decision = canonical_v6_decision(f"terminal-collision-{claim}")
    current = canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        )
    )
    payload = (
        {
            "outcome_status": "TERMINAL_MISSING",
            "path_replay_receipt": current["path_replay_receipt"],
        }
        if claim == "path"
        else {**current, "outcome_status": "TERMINAL_MISSING"}
    )

    assert (
        _truth_module().classify_canonical_return_truth(payload, decision=decision)
        == "LEGACY_OR_INCOMPLETE"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("return_truth_schema_version", "dawnstrike.alphaops.return_truth.v2"),
        ("cost_receipt_hash_sha256", "a" * 64),
        ("reconciliation_receipt_hash_sha256", "b" * 64),
        ("causal_decision_identity", {"kind": "alpha_v6_shadow_decision"}),
        ("source_bar_hash_sha256", "c" * 64),
        ("source_bar_count", 2),
        ("path_replay_schema_version", "dawnstrike.path_truth.v2"),
        ("eligibility_policy_version", "dawnstrike.alphaops-v6-eligibility.v2"),
        ("learning_eligible", False),
        ("attacker_extra", True),
    ),
)
def test_terminal_missing_rejects_every_nonminimal_truth_claim(
    field: str,
    value: object,
) -> None:
    decision = canonical_v6_decision(f"terminal-minimal-{field}")
    payload = {"outcome_status": "TERMINAL_MISSING", field: value}

    assert (
        _truth_module().classify_canonical_return_truth(payload, decision=decision)
        == "LEGACY_OR_INCOMPLETE"
    )
    assert _truth_module().canonical_return_truth_projection(
        payload,
        decision=decision,
    ) == {}


@pytest.mark.parametrize("payload", (None, [], "truth", 1, object()))
def test_return_truth_public_api_is_total_for_malformed_inputs(payload: object) -> None:
    decision = canonical_v6_decision("total-boundary")

    assert not _truth_module().canonical_return_truth_valid(
        payload,
        decision=decision,
    )
    assert _truth_module().canonical_return_truth_violations(
        payload,
        decision=decision,
    )
    assert (
        _truth_module().classify_canonical_return_truth(payload, decision=decision)
        == "LEGACY_OR_INCOMPLETE"
    )
    assert _truth_module().canonical_return_truth_projection(
        payload,
        decision=decision,
    ) == {}


def test_return_truth_public_api_is_total_for_cyclic_payload() -> None:
    decision = canonical_v6_decision("total-cyclic")
    cyclic: dict[str, object] = {}
    cyclic["path_replay_receipt"] = cyclic

    assert not _valid(cyclic, decision)
    assert _truth_module().canonical_return_truth_violations(
        cyclic,
        decision=decision,
    )
    assert (
        _truth_module().classify_canonical_return_truth(cyclic, decision=decision)
        == "LEGACY_OR_INCOMPLETE"
    )
    assert _truth_module().canonical_return_truth_projection(
        cyclic,
        decision=decision,
    ) == {}


@pytest.mark.parametrize("decision", (None, [], "decision", 1, object()))
def test_return_truth_public_api_is_total_for_malformed_decisions(
    decision: object,
) -> None:
    valid_decision = canonical_v6_decision("malformed-decision-boundary")
    outcome = canonical_return_outcome(
        causal_identity=causal_identity_from(
            valid_decision,
            kind="alpha_v6_shadow_decision",
        )
    )

    assert not _truth_module().canonical_return_truth_valid(
        outcome,
        decision=decision,
    )
    assert _truth_module().canonical_return_truth_violations(
        outcome,
        decision=decision,
    )


def _mutate(
    payload: dict[str, object],
    path: tuple[str, ...],
    value: object,
) -> dict[str, object]:
    mutated = copy.deepcopy(payload)
    cursor: dict[str, object] = mutated
    for key in path[:-1]:
        child = cursor[key]
        assert isinstance(child, dict)
        cursor = child
    cursor[path[-1]] = value
    return mutated


def _different_value(value: object) -> object:
    if value is None:
        return "attacker"
    if type(value) is bool:
        return not value
    if type(value) is int:
        return value + 1
    if type(value) is float:
        return value + 1.0
    if isinstance(value, str):
        return "attacker" if value != "attacker" else "attacker-2"
    if isinstance(value, list):
        return [*value, "attacker"]
    if isinstance(value, dict):
        return {**value, "attacker_extra": True}
    raise AssertionError(f"unsupported mutation fixture type: {type(value)!r}")


def _canonical_paper_selection() -> dict[str, object]:
    return {
        "selection_id": "selection-current",
        "scan_id": "scan-current",
        "signal_id": "signal-current",
        "ticker": "NOVA",
        "market_date": "2026-08-03",
        "strategy_id": "alphaops_v5",
        "strategy_version": "dawnstrike-alphaops-v5.0.0",
        "cohort": "official_telegram",
        "decision": "clean_edge",
        "selected_at": "2026-08-03T13:10:00+00:00",
        "input_hash_sha256": "8" * 64,
        "source_lineage_hash_sha256": "9" * 64,
        "delivery_identity": {
            "channel": "telegram",
            "delivery_status": "delivered",
        },
        "source_artifact_identity": "alpha-selection:selection-current",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _rehash_return_and_outcome(outcome: dict[str, object]) -> None:
    body = {
        "schema_version": outcome["return_truth_schema_version"],
        "path_replay_id": outcome["path_replay_id"],
        "path_replay_receipt_hash_sha256": outcome[
            "replay_receipt_hash_sha256"
        ],
        "source_artifact_hash_sha256": outcome["source_artifact_hash_sha256"],
        "source_bar_count": outcome["source_bar_count"],
        "replay_binding": outcome["replay_binding"],
        "cost_receipt_hash_sha256": outcome["cost_receipt_hash_sha256"],
        "benchmark_source_bar_hash_sha256": outcome[
            "benchmark_source_bar_hash_sha256"
        ],
        "secondary_benchmark_source_bar_hash_sha256": outcome[
            "secondary_benchmark_source_bar_hash_sha256"
        ],
        "reconciliation_receipt_hash_sha256": outcome[
            "reconciliation_receipt_hash_sha256"
        ],
        "after_cost_return_pct": outcome["after_cost_return_pct"],
        "net_excess_return_pct": outcome["net_excess_return_pct"],
        "causal_decision_identity": outcome["causal_decision_identity"],
        "eligibility_policy_version": outcome["eligibility_policy_version"],
        "retrospective_research_eligible": outcome[
            "retrospective_research_eligible"
        ],
        "prospective_promotion_eligible": outcome[
            "prospective_promotion_eligible"
        ],
        "evidence_cohort": outcome["evidence_cohort"],
        "no_lookahead": outcome["no_lookahead"],
        "validated_against_signal_timestamp": outcome[
            "validated_against_signal_timestamp"
        ],
        "research_only": outcome["research_only"],
        "broker_execution_enabled": outcome["broker_execution_enabled"],
    }
    truth_hash = hashlib.sha256(
        json.dumps(
            body,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    outcome["return_truth_hash_sha256"] = truth_hash
    identity = {
        "schema_version": outcome["return_truth_schema_version"],
        "return_truth_hash_sha256": truth_hash,
        "causal_decision_identity": outcome["causal_decision_identity"],
        "replay_binding": outcome["replay_binding"],
    }
    identity_hash = hashlib.sha256(
        json.dumps(
            identity,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    outcome["outcome_id"] = f"outcome-v2-{identity_hash}"


class _DeepcopyBomb:
    def __deepcopy__(self, _memo: object) -> object:
        raise AssertionError("unvalidated projection field was deep-copied")
