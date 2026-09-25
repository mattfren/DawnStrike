"""DS-02b: the capability tri-state must reach a real production surface.

``config_schema.CapabilityStatus`` / ``ScannerConfig.capability_report()``
were added in 9b6220ff (DS-02a) but, per independent review, were only
ever called from tests - never from any code path an operator actually
sees at runtime.  These tests exercise the genuine production surface
(``ScanResult.summary()``, which is what every ``scan`` / ``morning-run`` /
``live-scan`` invocation writes to ``scan_summary.json`` and prints via
``_print_scan_done``) and assert the capability status is actually there,
named, and structurally distinguishable - not just present as a buried
raw field in the 50-key config dump.
"""

from __future__ import annotations

import json

from intraday_scanner.config import STRATEGY_EVIDENCE_ENABLED_SPEC, ScannerConfig
from intraday_scanner.config_schema import CapabilityStatus
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.services.scan_service import ScanService

SUBSYSTEM = "strategy_decision_receipts"
KEY_NAME = STRATEGY_EVIDENCE_ENABLED_SPEC.name  # DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED


def _run_summary(config: ScannerConfig) -> dict:
    provider = CsvSnapshotProvider("sample_data/premarket_snapshot_sample.csv")
    result = ScanService(provider).run(config)
    return result.summary()


def test_missing_key_surfaces_disabled_missing_config_naming_the_key(tmp_path):
    # No override: the dataclass default mirrors what load_config() resolves
    # when DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED is absent from the runtime
    # source file - the exact real-world state described in the packet.
    config = ScannerConfig(database_path=tmp_path / "scanner.sqlite")
    summary = _run_summary(config)

    assert "capability_report" in summary, (
        "capability_report is missing from ScanResult.summary() - the "
        "tri-state status never reaches the operator-visible scan summary"
    )
    entry = summary["capability_report"][SUBSYSTEM]
    assert entry["status"] == CapabilityStatus.DISABLED_MISSING_CONFIG.value
    assert entry["key"] == KEY_NAME

    # Prominence: an unintended gap must show up in a dedicated, structural
    # place, not just as one field among many.
    assert SUBSYSTEM in summary.get("capability_config_gaps", {}), (
        "DISABLED_MISSING_CONFIG must be structurally prominent in the "
        "summary, not merely present somewhere in the config dump"
    )
    assert summary["capability_config_gaps"][SUBSYSTEM]["key"] == KEY_NAME


def test_operator_false_surfaces_disabled_by_operator_and_is_not_a_gap(tmp_path):
    config = ScannerConfig(database_path=tmp_path / "scanner.sqlite").with_overrides(
        strategy_evidence_enabled=False
    )
    summary = _run_summary(config)

    entry = summary["capability_report"][SUBSYSTEM]
    assert entry["status"] == CapabilityStatus.DISABLED_BY_OPERATOR.value
    assert entry["status"] != CapabilityStatus.DISABLED_MISSING_CONFIG.value

    # An operator's deliberate "off" must never light up the gap indicator.
    assert SUBSYSTEM not in summary.get("capability_config_gaps", {})


def test_enabled_surfaces_enabled_and_is_not_a_gap(tmp_path):
    config = ScannerConfig(database_path=tmp_path / "scanner.sqlite").with_overrides(
        strategy_evidence_enabled=True
    )
    summary = _run_summary(config)

    entry = summary["capability_report"][SUBSYSTEM]
    assert entry["status"] == CapabilityStatus.ENABLED.value
    assert SUBSYSTEM not in summary.get("capability_config_gaps", {})


def test_no_config_value_or_secret_appears_in_capability_output(tmp_path):
    config = ScannerConfig(
        database_path=tmp_path / "scanner.sqlite",
        openai_api_key="sk-should-never-appear",
    ).with_overrides(strategy_evidence_enabled=True)
    summary = _run_summary(config)

    entry = summary["capability_report"][SUBSYSTEM]
    # Only key name, capability enabled-bool and status string - never a
    # secret or an arbitrary config value.
    assert set(entry) == {"key", "enabled", "status"}
    serialized = json.dumps(summary["capability_report"])
    assert "sk-should-never-appear" not in serialized
