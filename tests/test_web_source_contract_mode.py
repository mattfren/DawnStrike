from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
    production_contract_status,
)


def _forward_config(user_agent: str) -> WebCollectionConfig:
    return WebCollectionConfig(
        enabled=True,
        respect_robots=True,
        user_agent=user_agent,
        timeout_seconds=15,
        rate_limit_seconds=5,
        save_raw=True,
        allowed_domains=("stockanalysis.com",),
        sources=(
            WebSourceConfig(
                name="stockanalysis_premarket",
                type="public_table_url",
                url="https://stockanalysis.com/markets/premarket/",
            ),
        ),
        production_contract=False,
    )


def test_forward_research_contract_is_explicit_and_ready_with_accountable_contact():
    result = production_contract_status(
        _forward_config("DawnstrikeResearchBot/1.0 contact: dawnstrikebot@gmail.com")
    )

    assert result["status"] == "FORWARD_RESEARCH_ONLY"
    assert result["ready"] is True
    assert result["historical_backfill_enabled"] is False
    assert result["broker_execution_enabled"] is False


def test_forward_research_template_placeholder_fails_closed():
    result = production_contract_status(
        _forward_config("DawnstrikeResearchBot/1.0 contact: REQUIRED_ACCOUNTABLE_EMAIL")
    )

    assert result["status"] == "FORWARD_RESEARCH_ONLY"
    assert result["ready"] is False
    assert result["violations"] == ["accountable_user_agent_contact_required"]


def test_forward_research_contract_rejects_fake_contact_and_unusable_source():
    config = _forward_config("Dawnstrike Contact: x")
    config = WebCollectionConfig(
        **{
            **config.__dict__,
            "sources": (WebSourceConfig(name="inbox", type="local_inbox"),),
        }
    )

    result = production_contract_status(config)

    assert result["ready"] is False
    assert "accountable_user_agent_contact_required" in result["violations"]
    assert "invalid_candidate_source:inbox" in result["violations"]


def test_production_contract_rejects_fake_contact_and_wrong_safety_types():
    config = WebCollectionConfig(
        enabled=True,
        respect_robots=True,
        user_agent="Dawnstrike Contact: x",
        timeout_seconds=15,
        rate_limit_seconds=5,
        save_raw=True,
        allowed_domains=("stockanalysis.com",),
        sources=(
            WebSourceConfig(
                name="candidate",
                type="public_table_url",
                url="https://stockanalysis.com/markets/premarket/",
            ),
            WebSourceConfig(name="nasdaq_halts", type="garbage"),
            WebSourceConfig(name="sec_edgar", type="garbage"),
        ),
        production_contract=True,
        primary_quote_source="alpaca",
        secondary_quote_source="yahoo",
        primary_benchmark="SPY",
        secondary_benchmark="IWM",
        universe_version="dated-v1",
    )

    result = production_contract_status(config)

    assert result["ready"] is False
    assert "accountable_user_agent_contact_required" in result["violations"]
    assert "invalid_safety_source_type:nasdaq_halts" in result["violations"]
    assert "invalid_safety_source_type:sec_edgar" in result["violations"]

    disabled = WebCollectionConfig(**{**config.__dict__, "enabled": False})
    disabled_result = production_contract_status(disabled)
    assert disabled_result["ready"] is False
    assert "web_collection_disabled" in disabled_result["violations"]
