"""OPVIEW-POLICY-STATE-UNAVAILABLE: missing/unreadable/stale policy state
must be a distinct, machine-readable operator-visible state - not folded
into ERROR and not folded into NO_ELIGIBLE_POLICY.

Context: ``72f0e08a`` gave the operator three distinguishable states
(``POLICY_ACTIVE``, ``NO_ELIGIBLE_POLICY``, ``ENTRIES_DISABLED_BY_OPERATOR``)
plus ``ERROR`` for a genuine runtime/data failure. But "the policy-
eligibility registry itself could not be read this run" (missing file,
corrupt/unreadable contents, or stale beyond
``POLICY_STATE_MAX_AGE_SECONDS``) was silently routed into ``ERROR``,
indistinguishable from a real fault such as a reconciliation mismatch. An
operator looking at "ERROR" could not tell "we don't know if there's an
eligible policy" from "the run broke". These tests exercise the real
production surface (``ScanResult.summary()``, same precedent as
``test_operator_no_eligible_policy.py``) and the pure decision function.
"""

from __future__ import annotations

import json

from intraday_scanner.config import ScannerConfig
from intraday_scanner.config_schema import (
    POLICY_STATE_MAX_AGE_SECONDS,
    OperatorRunState,
    evaluate_operator_run_state,
)
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.services.scan_service import ScanService

REQUIRED_CONTEXT_FIELDS = {
    "state",
    "version",
    "entry_mode",
    "entry_reason",
    "eligible_policy_count",
    "feed",
    "evaluation",
    "owned",
    "reconciliation_status",
    "errors",
}


def _run_summary(config: ScannerConfig) -> dict:
    provider = CsvSnapshotProvider("sample_data/premarket_snapshot_sample.csv")
    result = ScanService(provider).run(config)
    return result.summary()


def _base_config(tmp_path, **overrides):
    return ScannerConfig(database_path=tmp_path / "scanner.sqlite").with_overrides(**overrides)


# --- 1. Missing policy state ------------------------------------------------


def test_missing_policy_state_is_unavailable_not_error_not_no_eligible(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
        policy_state_present=False,
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    assert status["state"] == OperatorRunState.POLICY_STATE_UNAVAILABLE.value
    assert status["state"] != OperatorRunState.ERROR.value
    assert status["state"] != OperatorRunState.NO_ELIGIBLE_POLICY.value
    assert status["entry_reason"] == "policy_state_unavailable"


# --- 2. Unreadable / corrupt policy state -----------------------------------


def test_unreadable_policy_state_is_unavailable_not_error_not_no_eligible(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
        policy_state_readable=False,
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    assert status["state"] == OperatorRunState.POLICY_STATE_UNAVAILABLE.value
    assert status["state"] != OperatorRunState.ERROR.value
    assert status["state"] != OperatorRunState.NO_ELIGIBLE_POLICY.value


# --- 3. Stale policy state (beyond the documented threshold) ---------------


def test_stale_policy_state_beyond_threshold_is_unavailable(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
        policy_state_age_seconds=POLICY_STATE_MAX_AGE_SECONDS + 1.0,
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    assert status["state"] == OperatorRunState.POLICY_STATE_UNAVAILABLE.value
    assert status["state"] != OperatorRunState.ERROR.value
    assert status["state"] != OperatorRunState.NO_ELIGIBLE_POLICY.value


def test_policy_state_exactly_at_threshold_is_still_fresh(tmp_path):
    # At-threshold is documented as fresh (strictly-greater-than triggers
    # staleness), matching the existing feed-staleness comparison shape.
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
        policy_state_age_seconds=POLICY_STATE_MAX_AGE_SECONDS,
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    assert status["state"] == OperatorRunState.NO_ELIGIBLE_POLICY.value


# --- 4. A genuine runtime/data failure still -> ERROR, distinct from -------
#        POLICY_STATE_UNAVAILABLE, proving the two do not collapse.


def test_genuine_runtime_error_still_reports_error_even_with_unavailable_policy_state(
    tmp_path,
):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
        policy_state_present=False,
        operator_errors=("reconciliation_mismatch",),
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    assert status["state"] == OperatorRunState.ERROR.value
    assert status["state"] != OperatorRunState.POLICY_STATE_UNAVAILABLE.value


def test_genuine_runtime_error_alone_still_reports_error(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=("id-10-atr-band",),
        entries_enabled_override=True,
        operator_errors=("provider_exception",),
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    assert status["state"] == OperatorRunState.ERROR.value


# --- 5. The four other states are unchanged when policy state is fine ------


def test_policy_active_unchanged_when_policy_state_fine(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=("id-11-hypothetical-band",),
        entries_enabled_override=True,
    )
    status = _run_summary(config)["operator_run_status"]
    assert status["state"] == OperatorRunState.POLICY_ACTIVE.value


def test_no_eligible_policy_unchanged_when_policy_state_fine(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
    )
    status = _run_summary(config)["operator_run_status"]
    assert status["state"] == OperatorRunState.NO_ELIGIBLE_POLICY.value


def test_entries_disabled_by_operator_unchanged_when_policy_state_fine(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=("id-10-atr-band",),
        entries_enabled_override=False,
    )
    status = _run_summary(config)["operator_run_status"]
    assert status["state"] == OperatorRunState.ENTRIES_DISABLED_BY_OPERATOR.value


def test_error_unchanged_when_policy_state_fine(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
        operator_errors=("reconciliation_mismatch",),
    )
    status = _run_summary(config)["operator_run_status"]
    assert status["state"] == OperatorRunState.ERROR.value


def test_five_states_are_pairwise_distinct_values():
    values = {
        OperatorRunState.NO_ELIGIBLE_POLICY.value,
        OperatorRunState.ENTRIES_DISABLED_BY_OPERATOR.value,
        OperatorRunState.POLICY_STATE_UNAVAILABLE.value,
        OperatorRunState.ERROR.value,
        OperatorRunState.POLICY_ACTIVE.value,
    }
    assert len(values) == 5


# --- 6. Mutation check: a hardcoded return of POLICY_STATE_UNAVAILABLE -----
#        would be caught by at least one test above (namely #5's four
#        "unchanged" tests, and #4's ERROR-precedence tests).


def test_pure_function_hardcode_mutation_is_caught():
    # If evaluate_operator_run_state unconditionally returned
    # POLICY_STATE_UNAVAILABLE, this would fail - it must still be
    # computed from real inputs.
    assert (
        evaluate_operator_run_state(
            eligible_policy_count=2,
            entries_enabled=True,
            has_errors=False,
            policy_state_unavailable=False,
        )
        == OperatorRunState.POLICY_ACTIVE
    )
    assert (
        evaluate_operator_run_state(
            eligible_policy_count=2,
            entries_enabled=True,
            has_errors=True,
            policy_state_unavailable=True,
        )
        == OperatorRunState.ERROR
    )
    assert (
        evaluate_operator_run_state(
            eligible_policy_count=0,
            entries_enabled=False,
            has_errors=False,
            policy_state_unavailable=False,
        )
        == OperatorRunState.NO_ELIGIBLE_POLICY
    )
    assert (
        evaluate_operator_run_state(
            eligible_policy_count=0,
            entries_enabled=False,
            has_errors=False,
            policy_state_unavailable=True,
        )
        == OperatorRunState.POLICY_STATE_UNAVAILABLE
    )


# --- 7. No credential, account identifier, private path, or incident detail
#        appears in the surfaced payload for the new state. -----------------


def test_no_secret_or_private_detail_in_policy_state_unavailable_payload(tmp_path):
    config = ScannerConfig(
        database_path=tmp_path / "scanner.sqlite",
        openai_api_key="sk-should-never-appear",
        alpaca_api_key_id="AKIA-should-never-appear",
        alpaca_api_secret_key="secret-should-never-appear",
    ).with_overrides(
        eligible_policy_ids=(),
        entries_enabled_override=False,
        policy_state_present=False,
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    serialized = json.dumps(status)
    assert status["state"] == OperatorRunState.POLICY_STATE_UNAVAILABLE.value
    assert "sk-should-never-appear" not in serialized
    assert "AKIA-should-never-appear" not in serialized
    assert "secret-should-never-appear" not in serialized
    assert str(config.database_path) not in serialized
    # Whitelisted shape only - the new state must not have grown a new key
    # that could carry incident detail; same contract as before.
    assert set(status.keys()) == REQUIRED_CONTEXT_FIELDS
