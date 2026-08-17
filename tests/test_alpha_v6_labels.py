from __future__ import annotations

import copy
import hashlib
import json

import pytest

from intraday_scanner.alpha.v6.label_builder import build_label_families
from tests._alpha_path_truth import (
    canonical_ineligible_outcome,
    canonical_return_outcome,
    canonical_v6_decision,
    canonical_v6_label,
    causal_identity_from,
)

CURRENT_PATH_SCHEMA = "dawnstrike.path_truth.v2"
CURRENT_ELIGIBILITY_POLICY = "dawnstrike.alphaops-v6-eligibility.v2"


def test_v6_label_builder_keeps_missing_return_truth_null() -> None:
    labels = build_label_families(
        decision=_decision(),
        outcome={
            "outcome_id": "o1",
            "outcome_status": "TERMINAL_MISSING",
            "activation_status": "MISSING",
            "observed_at": "2026-08-04T01:00:00+00:00",
        },
    )
    by_family = {row["label_family"]: row for row in labels}

    assert by_family["net_return_after_cost"]["label_value"] is None
    assert by_family["net_return_after_cost"]["learning_eligible"] is False
    assert by_family["data_quality_failure"]["label_value"] == 1.0
    assert all(row["missing_truth_is_zero"] is False for row in labels)


def test_v6_return_labels_require_explicit_path_replay_when_present() -> None:
    labels = build_label_families(
        decision=_decision(),
        outcome={
            "outcome_id": "o2",
            "outcome_status": "COMPLETE_SOURCED",
            "activation_status": "ACTIVATED",
            "source_bar_hash_sha256": "bars",
            "path_replay_id": "",
            "learning_eligible": True,
            "net_return_pct": 1.0,
        },
    )
    return_label = next(row for row in labels if row["label_family"] == "net_return_after_cost")

    assert return_label["learning_eligible"] is False
    assert return_label["path_replay_id"] is None


def test_v6_return_labels_carry_lineage_and_require_complete_truth_contract() -> None:
    decision = {
        **_decision(),
        "input_hash_sha256": "8" * 64,
        "source_lineage_hash_sha256": "9" * 64,
        "point_in_time": {
            "all_inputs_observed_at_or_before_decision": True,
        },
    }
    outcome = _canonical_outcome(decision)
    labels = build_label_families(
        decision=decision,
        outcome=outcome,
    )
    return_label = next(
        row for row in labels if row["label_family"] == "net_return_after_cost"
    )

    assert return_label["learning_eligible"] is True
    assert return_label["source_artifact_hash_sha256"] == outcome[
        "source_bar_hash_sha256"
    ]
    assert return_label["benchmark_hash_sha256"] == outcome[
        "benchmark_source_bar_hash_sha256"
    ]
    assert return_label["path_replay_id"] == outcome["path_replay_id"]
    assert return_label["evidence_cohort"] == outcome["evidence_cohort"]
    assert return_label["return_truth_status"] == "COMPLETE"


@pytest.mark.parametrize(
    "missing_key",
    (
        "path_replay_schema_version",
        "path_replay_id",
        "path_replay_policy_version",
        "path_replay_policy_hash_sha256",
        "path_replay_receipt",
        "replay_input_manifest",
        "replay_input_hash_sha256",
        "replay_truth_hash_sha256",
        "replay_receipt_hash_sha256",
        "path_truth_status",
        "path_event",
        "source_bar_hash_sha256",
        "source_coverage_complete",
        "sequence_complete_through_exit",
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
        "benchmark_return_pct",
        "benchmark_symbol",
        "benchmark_source_bar_hash_sha256",
        "benchmark_independent_reconciliation_status",
        "secondary_benchmark_return_pct",
        "secondary_benchmark_symbol",
        "secondary_benchmark_source_bar_hash_sha256",
        "secondary_benchmark_independent_reconciliation_status",
        "independent_reconciliation_status",
        "reconciliation_schema_version",
        "reconciliation_receipt_id",
        "reconciliation_receipt_hash_sha256",
        "reconciliation_receipt",
        "causal_decision_identity",
        "retrospective_research_eligible",
        "prospective_promotion_eligible",
        "eligibility_policy_version",
    ),
)
def test_v6_modern_return_contract_fails_closed_on_each_missing_truth(
    missing_key: str,
) -> None:
    outcome = _canonical_outcome()
    outcome.pop(missing_key)

    labels = build_label_families(decision=_decision(), outcome=outcome)
    return_labels = [
        row
        for row in labels
        if row["label_family"]
        in {
            "net_return_after_cost",
            "benchmark_relative_excess_return",
            "tail_loss_event",
        }
    ]

    assert all(row["learning_eligible"] is False for row in return_labels)
    assert all(row["return_truth_status"] != "LEGACY_CONTRACT" for row in return_labels)


@pytest.mark.parametrize(
    ("key", "blank"),
    (
        ("path_replay_schema_version", ""),
        ("path_replay_id", " "),
        ("path_replay_policy_version", ""),
        ("path_replay_policy_hash_sha256", "not-a-hash"),
        ("path_replay_receipt", {}),
        ("replay_input_manifest", {}),
        ("replay_input_hash_sha256", ""),
        ("replay_truth_hash_sha256", ""),
        ("replay_receipt_hash_sha256", ""),
        ("path_truth_status", ""),
        ("path_event", ""),
        ("source_bar_hash_sha256", ""),
        ("source_coverage_complete", None),
        ("sequence_complete_through_exit", None),
        ("return_truth_schema_version", ""),
        ("return_truth_hash_sha256", ""),
        ("cost_schema_version", ""),
        ("cost_receipt_id", ""),
        ("cost_receipt_hash_sha256", ""),
        ("cost_receipt", {}),
        ("observed_cost_model_identity", ""),
        ("modeled_cost_model_identity", ""),
        ("cost_components", {}),
        ("benchmark_symbol", ""),
        ("benchmark_return_pct", None),
        ("benchmark_source_bar_hash_sha256", ""),
        ("benchmark_independent_reconciliation_status", ""),
        ("secondary_benchmark_symbol", ""),
        ("secondary_benchmark_return_pct", None),
        ("secondary_benchmark_source_bar_hash_sha256", ""),
        ("secondary_benchmark_independent_reconciliation_status", ""),
        ("reconciliation_schema_version", ""),
        ("reconciliation_receipt_id", ""),
        ("reconciliation_receipt_hash_sha256", ""),
        ("reconciliation_receipt", {}),
        ("independent_reconciliation_status", ""),
        ("causal_decision_identity", None),
        ("retrospective_research_eligible", None),
        ("prospective_promotion_eligible", None),
        ("eligibility_policy_version", ""),
    ),
)
def test_v6_modern_return_contract_rejects_blank_or_coercion_only_truth(
    key: str,
    blank: object,
) -> None:
    outcome = {**_canonical_outcome(), key: blank}
    labels = build_label_families(decision=_decision(), outcome=outcome)

    assert all(
        row["learning_eligible"] is False
        for row in labels
        if row["label_family"]
        not in {"data_quality_failure"}
    )


@pytest.mark.parametrize(
    ("path", "wrong"),
    (
        (("path_replay_schema_version",), "dawnstrike.path_truth.v999"),
        (("path_replay_policy_version",), "attacker-policy"),
        (("path_replay_policy_hash_sha256",), "f" * 64),
        (("replay_input_hash_sha256",), "f" * 64),
        (("replay_truth_hash_sha256",), "f" * 64),
        (("replay_receipt_hash_sha256",), "f" * 64),
        (("path_replay_id",), "path-v2-" + "f" * 64),
        (("source_artifact_identity",), "bars:ATTACKER:2026-08-03"),
        (("source_artifact_hash_sha256",), "f" * 64),
        (("source_coverage_complete",), False),
        (("source_conflict",), True),
        (("corporate_action_unresolved",), True),
        (("sequence_complete_through_exit",), False),
        (("path_truth_status",), "RESOLVED_STOP_FIRST"),
        (("path_event",), "STOP"),
        (("entry_price",), 99.0),
        (("exit_price",), 999.0),
        (("return_truth_schema_version",), "attacker-return-v9"),
        (("return_truth_hash_sha256",), "f" * 64),
        (("cost_schema_version",), "attacker-cost-v9"),
        (("cost_receipt_id",), "attacker-cost"),
        (("cost_receipt_hash_sha256",), "f" * 64),
        (("cost_receipt", "after_cost_return_pct"), 99.0),
        (("observed_cost_model_identity",), "attacker-model"),
        (("modeled_cost_model_identity",), "attacker-model"),
        (("cost_components", "fee_bps_per_side"), -1.0),
        (("after_cost_return_pct",), 99.0),
        (("benchmark_symbol",), "QQQ"),
        (("benchmark_return_pct",), 99.0),
        (("benchmark_source_bar_hash_sha256",), "f" * 64),
        (("benchmark_independent_reconciliation_status",), "FAILED"),
        (("secondary_benchmark_symbol",), "QQQ"),
        (("secondary_benchmark_return_pct",), 99.0),
        (("secondary_benchmark_source_bar_hash_sha256",), "f" * 64),
        (("secondary_benchmark_independent_reconciliation_status",), "PENDING"),
        (("reconciliation_schema_version",), "attacker-recon-v9"),
        (("reconciliation_receipt_id",), "attacker-recon"),
        (("reconciliation_receipt_hash_sha256",), "f" * 64),
        (("reconciliation_receipt", "status"), "FAILED"),
        (("independent_reconciliation_status",), "FAILED"),
        (("causal_decision_identity", "decision_id"), "attacker-decision"),
        (("eligibility_policy_version",), "attacker-eligibility-v9"),
        (("retrospective_research_eligible",), False),
        (("prospective_promotion_eligible",), "true"),
        (("evidence_cohort",), "attacker-cohort"),
        (("no_lookahead",), False),
        (("research_only",), False),
        (("broker_execution_enabled",), True),
    ),
)
def test_v6_current_contract_rejects_each_wrong_nonblank_dimension(
    path: tuple[str, ...],
    wrong: object,
) -> None:
    outcome = _mutate(_canonical_outcome(), path, wrong)
    labels = build_label_families(decision=_decision(), outcome=outcome)

    assert all(
        row["learning_eligible"] is False
        for row in labels
        if row["label_family"] != "data_quality_failure"
    )


def test_v6_legacy_contract_and_old_path_status_are_quarantined() -> None:
    legacy = {
        "outcome_id": "legacy-o3",
        "outcome_status": "COMPLETE_SOURCED",
        "activation_status": "ACTIVATED",
        "source_bar_hash_sha256": "b" * 64,
        "path_replay_id": "legacy-path",
        "path_truth_status": "ENTRY_BAR_AMBIGUOUS",
        "learning_eligible": True,
        "net_return_pct": 1.0,
        "net_excess_return_pct": 0.5,
    }

    label = next(
        row
        for row in build_label_families(decision=_decision(), outcome=legacy)
        if row["label_family"] == "net_return_after_cost"
    )

    assert label["learning_eligible"] is False
    assert label["return_truth_contract_present"] is False
    assert label["return_truth_status"] == "MISSING_CURRENT_CONTRACT"


def test_v6_label_builder_rejects_120_forged_boolean_outcomes() -> None:
    eligible = []
    for index in range(120):
        decision = {
            **_decision(),
            "decision_id": f"forged-{index}",
        }
        outcome = {
            "outcome_id": f"forged-outcome-{index}",
            "outcome_status": "COMPLETE_SOURCED",
            "activation_status": "ACTIVATED",
            "source_bar_hash_sha256": "a" * 64,
            "after_cost_return_pct": 2.0,
            "net_excess_return_pct": 1.5,
            "learning_eligible": True,
            "retrospective_research_eligible": True,
            "prospective_promotion_eligible": True,
        }
        eligible.extend(
            row
            for row in build_label_families(decision=decision, outcome=outcome)
            if row["label_family"] != "data_quality_failure"
            and row["learning_eligible"] is True
        )

    assert eligible == []


def test_v6_after_cost_label_never_falls_back_to_legacy_net_return() -> None:
    outcome = _canonical_outcome()
    outcome.pop("after_cost_return_pct")
    outcome["net_return_pct"] = 99.0

    label = next(
        row
        for row in build_label_families(decision=_decision(), outcome=outcome)
        if row["label_family"] == "net_return_after_cost"
    )

    assert label["label_value"] is None
    assert label["learning_eligible"] is False


def test_v6_label_identity_binds_schema_policy_and_full_lineage() -> None:
    current = _canonical_outcome()
    legacy = {
        **current,
        "path_replay_schema_version": "dawnstrike.path_truth.v1",
        "eligibility_policy_version": "dawnstrike.alphaops-v6-eligibility.v1",
    }
    current_label = next(
        row
        for row in build_label_families(decision=_decision(), outcome=current)
        if row["label_family"] == "net_return_after_cost"
    )
    legacy_label = next(
        row
        for row in build_label_families(decision=_decision(), outcome=legacy)
        if row["label_family"] == "net_return_after_cost"
    )

    assert current_label["label_id"] != legacy_label["label_id"]
    assert current_label["label_schema_version"].endswith("v2")
    assert current_label["eligibility_policy_version"] == CURRENT_ELIGIBILITY_POLICY
    identity = {
        "schema_version": current_label["label_schema_version"],
        "decision_id": current_label["decision_id"],
        "family": current_label["label_family"],
        "value": current_label["label_value"],
        "truth_lineage_hash_sha256": current_label[
            "truth_lineage_hash_sha256"
        ],
    }
    identity_hash = hashlib.sha256(
        json.dumps(identity, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert current_label["label_id"] == f"v6l-v2-{identity_hash}"
    payload_hash = hashlib.sha256(
        json.dumps(
            {**identity, "label_id": current_label["label_id"]},
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert current_label["label_payload_hash_sha256"] == payload_hash


def test_v6_mfe_mae_labels_require_exact_excursion_sequence() -> None:
    bounded = _canonical_outcome()
    bounded.update(
        {
            "excursion_exact": False,
            "mfe_pct": 5.0,
            "mae_pct": -2.0,
            "bounds": {"mfe_upper": 12.0, "mae_lower": 8.0},
        }
    )
    labels = {
        row["label_family"]: row
        for row in build_label_families(decision=_decision(), outcome=bounded)
    }

    assert labels["mfe_pct"]["label_value"] is None
    assert labels["mfe_pct"]["learning_eligible"] is False
    assert labels["mae_pct"]["label_value"] is None
    assert labels["mae_pct"]["learning_eligible"] is False


def test_v6_path_contract_gates_activation_and_fill_labels_too() -> None:
    missing_path = _canonical_outcome()
    missing_path.pop("path_replay_id")
    labels = {
        row["label_family"]: row
        for row in build_label_families(decision=_decision(), outcome=missing_path)
    }

    assert labels["activation"]["learning_eligible"] is False
    assert labels["simulated_fill_feasibility"]["learning_eligible"] is False
    assert labels["data_quality_failure"]["learning_eligible"] is True


@pytest.mark.parametrize(
    "case",
    (
        "entry_censored",
        "same_censored",
        "missing_interval",
        "halt",
        "source_conflict",
        "corporate_action",
    ),
)
def test_v6_all_censored_paths_are_audit_only(case: str) -> None:
    decision = _decision()
    outcome = canonical_ineligible_outcome(
        case=case,
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )
    labels = build_label_families(decision=decision, outcome=outcome)

    assert all(
        row["learning_eligible"] is False
        for row in labels
        if row["label_family"] != "data_quality_failure"
    )


def test_v6_not_triggered_allows_activation_only_without_return_contract() -> None:
    decision = _decision()
    outcome = canonical_ineligible_outcome(
        case="not_triggered",
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )
    labels = {
        row["label_family"]: row
        for row in build_label_families(decision=decision, outcome=outcome)
    }

    assert labels["activation"]["learning_eligible"] is True
    assert labels["activation"]["label_value"] == 0.0
    assert labels["net_return_after_cost"]["learning_eligible"] is False
    assert labels["net_return_after_cost"]["label_value"] is None


def test_v6_retro_only_truth_is_preserved_but_not_promotion_eligible() -> None:
    decision = _decision()
    outcome = canonical_return_outcome(
        prospective=False,
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )
    labels = build_label_families(decision=decision, outcome=outcome)

    assert all(row["retrospective_research_eligible"] is True for row in labels)
    assert all(row["prospective_promotion_eligible"] is False for row in labels)


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("path_replay_id", "path-v2-" + "4" * 64),
        ("path_replay_policy_hash_sha256", "4" * 64),
        ("replay_receipt_hash_sha256", "4" * 64),
        ("return_truth_hash_sha256", "4" * 64),
        ("source_bar_hash_sha256", "c" * 64),
        ("cost_receipt_id", "cost-4"),
        ("cost_receipt_hash_sha256", "4" * 64),
        ("observed_cost_model_identity", "observed-v4"),
        ("modeled_cost_model_identity", "modeled-v4"),
        ("cost_components", {"fee_bps_per_side": 2.0}),
        ("benchmark_return_pct", 2.0),
        ("benchmark_source_bar_hash_sha256", "d" * 64),
        ("benchmark_independent_reconciliation_status", "FAILED"),
        ("secondary_benchmark_return_pct", 2.0),
        ("secondary_benchmark_source_bar_hash_sha256", "e" * 64),
        ("secondary_benchmark_independent_reconciliation_status", "FAILED"),
        ("causal_decision_identity", {"kind": "changed"}),
        ("reconciliation_receipt_id", "recon-4"),
        ("reconciliation_receipt_hash_sha256", "e" * 64),
        ("independent_reconciliation_status", "FAILED"),
        ("retrospective_research_eligible", False),
        ("prospective_promotion_eligible", False),
        ("after_cost_return_pct", 1.2345),
        ("eligibility_policy_version", "dawnstrike.alphaops-v6-eligibility.v3"),
        ("path_replay_schema_version", "dawnstrike.path_truth.v3"),
    ),
)
def test_v6_label_identity_changes_with_every_truth_lineage_dimension(
    field: str,
    changed: object,
) -> None:
    baseline = _canonical_outcome()
    mutated = {**baseline, field: changed}
    baseline_id = next(
        row["label_id"]
        for row in build_label_families(decision=_decision(), outcome=baseline)
        if row["label_family"] == "net_return_after_cost"
    )
    mutated_id = next(
        row["label_id"]
        for row in build_label_families(decision=_decision(), outcome=mutated)
        if row["label_family"] == "net_return_after_cost"
    )

    assert baseline_id != mutated_id


@pytest.mark.parametrize(
    ("path", "changed"),
    (
        (("experiment_assignment", "arm"), "candidate"),
        (("safety_vetoes",), ["source_conflict"]),
        (("evidence_cohort",), "changed-cohort"),
    ),
)
def test_v6_authentic_label_identity_binds_decision_context(
    path: tuple[str, ...],
    changed: object,
) -> None:
    baseline_decision = {
        **canonical_v6_decision("label-context"),
        "experiment_assignment": {
            "experiment_id": "experiment-v2",
            "arm": "baseline",
            "configuration_hash_sha256": "d" * 64,
        },
    }
    changed_decision = copy.deepcopy(baseline_decision)
    cursor: dict[str, object] = changed_decision
    for key in path[:-1]:
        child = cursor[key]
        assert isinstance(child, dict)
        cursor = child
    cursor[path[-1]] = changed
    baseline = canonical_v6_label(baseline_decision, value=1.0)
    updated = canonical_v6_label(changed_decision, value=1.0)

    assert baseline["label_id"] != updated["label_id"]
    assert baseline["label_payload_hash_sha256"] != updated[
        "label_payload_hash_sha256"
    ]
    assert baseline["truth_lineage_hash_sha256"] != updated[
        "truth_lineage_hash_sha256"
    ]


def _decision() -> dict[str, object]:
    return canonical_v6_decision("d1")


def _canonical_outcome(
    decision: dict[str, object] | None = None,
) -> dict[str, object]:
    bound_decision = decision or _decision()
    return {
        **canonical_return_outcome(
            causal_identity=causal_identity_from(
                bound_decision,
                kind="alpha_v6_shadow_decision",
            )
        ),
        "observed_at": "2026-08-03T21:00:00+00:00",
    }


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
