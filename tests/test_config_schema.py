"""DS-02a: required-key/schema validation and capability-status tests.

These exercise the defect described in the DS-02 controller scope: an
absent capability key (``DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED``) must never
be indistinguishable from an operator explicitly disabling it, and a truly
required key must fail loudly instead of silently defaulting.
"""

from __future__ import annotations

import pytest

from intraday_scanner.config import STRATEGY_EVIDENCE_ENABLED_SPEC, load_config
from intraday_scanner.config_schema import (
    CapabilityStatus,
    ConfigKeySpec,
    resolve_capability_bool,
)
from intraday_scanner.errors import ConfigError


# --- Generic schema mechanism (not tied to any single production key) -----


def test_missing_required_key_fails_loudly_and_names_the_key():
    spec = ConfigKeySpec(
        name="DAWNSTRIKE_TEST_REQUIRED_KEY",
        kind="bool",
        required=True,
        subsystem="test_subsystem",
        default="false",
    )
    with pytest.raises(ConfigError) as excinfo:
        resolve_capability_bool(spec, {})
    message = str(excinfo.value)
    assert "DAWNSTRIKE_TEST_REQUIRED_KEY" in message
    assert "test_subsystem" in message


def test_required_key_present_does_not_raise():
    spec = ConfigKeySpec(
        name="DAWNSTRIKE_TEST_REQUIRED_KEY",
        kind="bool",
        required=True,
        subsystem="test_subsystem",
        default="false",
    )
    resolved = resolve_capability_bool(spec, {"DAWNSTRIKE_TEST_REQUIRED_KEY": "true"})
    assert resolved.value is True
    assert resolved.status is CapabilityStatus.ENABLED


def test_malformed_boolean_value_fails_loudly_naming_the_key():
    spec = ConfigKeySpec(
        name="DAWNSTRIKE_TEST_FLAG",
        kind="bool",
        required=False,
        subsystem="test_subsystem",
        default="false",
    )
    with pytest.raises(ConfigError) as excinfo:
        resolve_capability_bool(spec, {"DAWNSTRIKE_TEST_FLAG": "maybe"})
    message = str(excinfo.value)
    assert "DAWNSTRIKE_TEST_FLAG" in message
    assert "maybe" in message


def test_explicit_false_reports_disabled_by_operator():
    spec = ConfigKeySpec(
        name="DAWNSTRIKE_TEST_FLAG",
        kind="bool",
        required=False,
        subsystem="test_subsystem",
        default="false",
    )
    resolved = resolve_capability_bool(spec, {"DAWNSTRIKE_TEST_FLAG": "false"})
    assert resolved.value is False
    assert resolved.status is CapabilityStatus.DISABLED_BY_OPERATOR
    assert resolved.source == "explicit"
    assert resolved.defaulted is False


def test_absent_key_reports_disabled_missing_config_not_operator():
    spec = ConfigKeySpec(
        name="DAWNSTRIKE_TEST_FLAG",
        kind="bool",
        required=False,
        subsystem="test_subsystem",
        default="false",
    )
    resolved = resolve_capability_bool(spec, {})
    assert resolved.value is False
    assert resolved.status is CapabilityStatus.DISABLED_MISSING_CONFIG
    assert resolved.status is not CapabilityStatus.DISABLED_BY_OPERATOR
    assert resolved.source == "absent"
    assert resolved.defaulted is True


# --- Wired into the real DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED key ----------


def test_strategy_evidence_key_absent_is_disabled_missing_config(tmp_path, monkeypatch):
    monkeypatch.delenv("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    config = load_config(env_file)

    assert config.strategy_evidence_enabled is False
    assert config.strategy_evidence_status == CapabilityStatus.DISABLED_MISSING_CONFIG.value
    report = config.capability_report()
    assert report["strategy_decision_receipts"]["status"] == "DISABLED_MISSING_CONFIG"
    assert report["strategy_decision_receipts"]["key"] == STRATEGY_EVIDENCE_ENABLED_SPEC.name


def test_strategy_evidence_key_explicit_false_is_disabled_by_operator(tmp_path, monkeypatch):
    monkeypatch.delenv("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED=false\n", encoding="utf-8")

    config = load_config(env_file)

    assert config.strategy_evidence_enabled is False
    assert config.strategy_evidence_status == CapabilityStatus.DISABLED_BY_OPERATOR.value
    assert config.strategy_evidence_status != CapabilityStatus.DISABLED_MISSING_CONFIG.value


def test_strategy_evidence_key_explicit_true_is_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED=true\n", encoding="utf-8")

    config = load_config(env_file)

    assert config.strategy_evidence_enabled is True
    assert config.strategy_evidence_status == CapabilityStatus.ENABLED.value


def test_strategy_evidence_key_malformed_value_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED=maybe\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(env_file)
    message = str(excinfo.value)
    assert "DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED" in message
    assert "maybe" in message


def test_strategy_evidence_key_is_not_declared_required():
    # Established fact: the runtime deployment has never set this key, so
    # declaring it required would hard-fail every existing deployment at
    # startup. Its absence must be loud (DISABLED_MISSING_CONFIG, above),
    # not fatal.
    assert STRATEGY_EVIDENCE_ENABLED_SPEC.required is False


def test_optional_key_absent_default_applied_and_visible(tmp_path, monkeypatch):
    # strategy_evidence_shadow_only is optional with a "true" default. When
    # absent, the default is still applied, and the config report can show
    # the resolved value (this asserts the value on the loaded config,
    # which is the observable surface every consumer actually reads).
    monkeypatch.delenv("DAWNSTRIKE_STRATEGY_EVIDENCE_SHADOW_ONLY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    config = load_config(env_file)

    assert config.strategy_evidence_shadow_only is True


def test_with_overrides_keeps_capability_status_in_lockstep():
    config = load_config(strategy_evidence_enabled=True)
    assert config.strategy_evidence_enabled is True
    assert config.strategy_evidence_status == CapabilityStatus.ENABLED.value

    disabled = config.with_overrides(strategy_evidence_enabled=False)
    assert disabled.strategy_evidence_status == CapabilityStatus.DISABLED_BY_OPERATOR.value
