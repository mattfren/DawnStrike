from __future__ import annotations

import json
from pathlib import Path

from intraday_scanner.config import ScannerConfig, load_config
from intraday_scanner.providers.base import HistoricalIntradayProvider
from intraday_scanner.providers.massive_market_data_provider import MassiveMarketDataProvider
from scripts.probe_intraday_provider import build_probe_receipt


def test_massive_without_approved_key_emits_external_entitlement_block() -> None:
    config = ScannerConfig()
    provider = MassiveMarketDataProvider(config)

    assert isinstance(provider, HistoricalIntradayProvider)
    receipt = build_probe_receipt(
        provider,
        config=config,
        operator_metadata={},
        symbols=("TST",),
        code_sha="test",
    )

    assert receipt["probe_status"] == "BLOCKED_EXTERNAL_MARKET_DATA_ENTITLEMENT"
    assert receipt["credential_present"] is False


def test_probe_receipt_is_sanitized_and_records_feed_pagination_and_retention() -> None:
    config = ScannerConfig(alpaca_api_key_id="id-secret", alpaca_api_secret_key="secret")
    provider = _FakeProvider()
    receipt = build_probe_receipt(
        provider,
        config=config,
        operator_metadata={"approved_plan": True, "retention_allowed": True},
        symbols=("TST", "ABC"),
        code_sha="test",
    )

    serialized = json.dumps(receipt, sort_keys=True)
    assert "id-secret" not in serialized
    assert "secret" not in serialized
    assert receipt["feed"] == "sip"
    assert receipt["pagination_limits"]["max_pages"] == 100
    assert receipt["raw_data_retention_permitted"] is True
    assert receipt["receipt_hash_sha256"]


def test_config_loads_evidence_root_massive_alias_and_page_bounds(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "DAWNSTRIKE_INTRADAY_EVIDENCE_ROOT=retained\n"
        "POLYGON_API_KEY=compat-key\n"
        "DAWNSTRIKE_INTRADAY_PAGE_LIMIT=25\n"
        "DAWNSTRIKE_INTRADAY_MAX_PAGES=4\n",
        encoding="utf-8",
    )

    config = load_config(env)

    assert str(config.intraday_evidence_root) == "retained"
    assert config.massive_api_key == "compat-key"
    assert config.historical_intraday_page_limit == 25
    assert config.historical_intraday_max_pages == 4


class _FakeProvider:
    provider_name = "fixture"
    feed = "sip"

    def capability_probe(self, config: ScannerConfig) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "feed": self.feed,
            "credential_present": True,
            "capabilities": {"bars": "fixture", "pagination": True},
        }


def test_protocol_requires_all_historical_capability_methods() -> None:
    required = {
        "capability_probe",
        "get_bars_page",
        "get_trades_page",
        "get_quotes_page",
        "get_corporate_actions_page",
    }
    assert required <= set(HistoricalIntradayProvider.__annotations__) | {
        name for name in dir(HistoricalIntradayProvider) if not name.startswith("_")
    }
