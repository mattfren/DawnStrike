"""OPVIEW-NO-ELIGIBLE-POLICY: NO_ELIGIBLE_POLICY must be a distinct,
machine-readable, actually-computed operator-visible state.

Context: both strategy candidates evaluated (ID 08 noise-band, ID 10
ATR-band) were rejected. The strategy search is stopped and no eligible
trading policy exists, so new paper entries are disabled. An operator
looking at the system today cannot currently tell *why* nothing trades -
whether it is this, an operator's own toggle, or a fault. These tests
exercise the genuine production surface (``ScanResult.summary()``, same
surface ``capability_report`` was proven against in
``test_capability_report_surfaced.py``) and assert the three states never
collapse into one value, and that NO_ELIGIBLE_POLICY is computed from real
inputs rather than hardcoded.
"""

from __future__ import annotations

import json

from intraday_scanner.config import ScannerConfig
from intraday_scanner.config_schema import OperatorRunState, evaluate_operator_run_state
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


# --- 1. NO_ELIGIBLE_POLICY is surfaced when no policy is eligible ---------


def test_no_eligible_policy_is_surfaced_in_scan_summary(tmp_path):
    # The real production state: both candidates rejected, nothing eligible.
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
        release_sha="ded04d3cf9bcfceadbed2f5c532ce7b013573af6",
    )
    summary = _run_summary(config)

    assert "operator_run_status" in summary, (
        "operator_run_status is missing from ScanResult.summary() - the "
        "NO_ELIGIBLE_POLICY state never reaches the operator-visible scan "
        "summary"
    )
    status = summary["operator_run_status"]
    assert status["state"] == OperatorRunState.NO_ELIGIBLE_POLICY.value
    assert status["eligible_policy_count"] == 0
    # Structural prominence, same reasoning as capability_config_gaps.
    assert summary["no_eligible_policy"] is True


# --- 2. Distinct from operator-disabled entries and from an error state ---


def test_no_eligible_policy_is_distinct_from_operator_disabled_entries(tmp_path):
    # A policy IS eligible here; the operator simply has not armed entries.
    # This must be a different value from "nothing is eligible".
    config = _base_config(
        tmp_path,
        eligible_policy_ids=("id-10-atr-band",),
        entries_enabled_override=False,
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    assert status["state"] == OperatorRunState.ENTRIES_DISABLED_BY_OPERATOR.value
    assert status["state"] != OperatorRunState.NO_ELIGIBLE_POLICY.value
    assert summary["no_eligible_policy"] is False


def test_no_eligible_policy_is_distinct_from_error_state(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
        operator_errors=("reconciliation_mismatch",),
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    # An error must win over "no eligible policy" naming, even though zero
    # policies are eligible in both scenarios - the operator needs to know
    # the system may not be trustworthy right now, not that it is healthy
    # with nothing to trade.
    assert status["state"] == OperatorRunState.ERROR.value
    assert status["state"] != OperatorRunState.NO_ELIGIBLE_POLICY.value
    assert status["state"] != OperatorRunState.ENTRIES_DISABLED_BY_OPERATOR.value


def test_three_states_are_pairwise_distinct_values():
    values = {
        OperatorRunState.NO_ELIGIBLE_POLICY.value,
        OperatorRunState.ENTRIES_DISABLED_BY_OPERATOR.value,
        OperatorRunState.ERROR.value,
        OperatorRunState.POLICY_ACTIVE.value,
    }
    assert len(values) == 4


# --- 3. Operating context fields are present -------------------------------


def test_operating_context_fields_are_present(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=(),
        entries_enabled_override=False,
        release_sha="ded04d3cf9bcfceadbed2f5c532ce7b013573af6",
        feed_name="alpaca_iex",
        feed_age_seconds=45.0,
        last_evaluation_at="2026-09-19T20:00:00Z",
        next_evaluation_at="2026-09-22T13:30:00Z",
        owned_order_count=0,
        owned_position_count=0,
        reconciliation_status="reconciled",
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    assert REQUIRED_CONTEXT_FIELDS.issubset(status.keys())
    assert status["version"]["release_sha"] == "ded04d3cf9bcfceadbed2f5c532ce7b013573af6"
    assert status["feed"]["name"] == "alpaca_iex"
    assert status["feed"]["age_seconds"] == 45.0
    assert status["feed"]["stale"] is False
    assert status["evaluation"]["last_evaluation_at"] == "2026-09-19T20:00:00Z"
    assert status["evaluation"]["next_evaluation_at"] == "2026-09-22T13:30:00Z"
    assert status["owned"]["order_count"] == 0
    assert status["owned"]["position_count"] == 0
    assert status["reconciliation_status"] == "reconciled"
    assert status["errors"] == []


# --- 4. A hypothetical eligible policy would NOT report NO_ELIGIBLE_POLICY,
#        proving the state is computed, not hardcoded. --------------------


def test_hypothetical_eligible_policy_with_entries_enabled_is_policy_active(tmp_path):
    config = _base_config(
        tmp_path,
        eligible_policy_ids=("id-11-hypothetical-band",),
        entries_enabled_override=True,
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    assert status["state"] == OperatorRunState.POLICY_ACTIVE.value
    assert status["state"] != OperatorRunState.NO_ELIGIBLE_POLICY.value
    assert status["eligible_policy_count"] == 1
    assert summary["no_eligible_policy"] is False


def test_evaluate_operator_run_state_is_a_pure_function_of_its_inputs():
    # Directly exercises the decision function so the production wiring
    # above cannot be hiding a hardcoded return value.
    assert (
        evaluate_operator_run_state(
            eligible_policy_count=0, entries_enabled=False, has_errors=False
        )
        == OperatorRunState.NO_ELIGIBLE_POLICY
    )
    assert (
        evaluate_operator_run_state(
            eligible_policy_count=0, entries_enabled=True, has_errors=False
        )
        == OperatorRunState.NO_ELIGIBLE_POLICY
    )
    assert (
        evaluate_operator_run_state(
            eligible_policy_count=2, entries_enabled=False, has_errors=False
        )
        == OperatorRunState.ENTRIES_DISABLED_BY_OPERATOR
    )
    assert (
        evaluate_operator_run_state(
            eligible_policy_count=2, entries_enabled=True, has_errors=False
        )
        == OperatorRunState.POLICY_ACTIVE
    )
    assert (
        evaluate_operator_run_state(
            eligible_policy_count=2, entries_enabled=True, has_errors=True
        )
        == OperatorRunState.ERROR
    )


# --- 5. No secret or config value appears in the surfaced output ----------


def test_no_secret_appears_in_operator_run_status(tmp_path):
    config = ScannerConfig(
        database_path=tmp_path / "scanner.sqlite",
        openai_api_key="sk-should-never-appear",
        alpaca_api_key_id="AKIA-should-never-appear",
        alpaca_api_secret_key="secret-should-never-appear",
    ).with_overrides(
        eligible_policy_ids=(),
        entries_enabled_override=False,
    )
    summary = _run_summary(config)
    status = summary["operator_run_status"]

    serialized = json.dumps(status)
    assert "sk-should-never-appear" not in serialized
    assert "AKIA-should-never-appear" not in serialized
    assert "secret-should-never-appear" not in serialized
    # Whitelisted shape only - no arbitrary config dump leaking through.
    assert set(status.keys()) == REQUIRED_CONTEXT_FIELDS
