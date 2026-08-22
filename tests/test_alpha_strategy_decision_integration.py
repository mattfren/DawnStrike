from intraday_scanner.config import load_config


def test_strategy_evidence_defaults_disabled_and_shadow_only(monkeypatch) -> None:
    monkeypatch.delenv("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED", raising=False)
    monkeypatch.delenv("DAWNSTRIKE_STRATEGY_EVIDENCE_SHADOW_ONLY", raising=False)
    config = load_config()
    assert config.strategy_evidence_enabled is False
    assert config.strategy_evidence_shadow_only is True


def test_strategy_evidence_flags_can_be_enabled_without_broker_execution(monkeypatch) -> None:
    monkeypatch.setenv("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED", "true")
    monkeypatch.setenv("DAWNSTRIKE_STRATEGY_EVIDENCE_SHADOW_ONLY", "true")
    config = load_config()
    assert config.strategy_evidence_enabled is True
    assert config.strategy_evidence_shadow_only is True
