from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.services.indeterminate_research_service import (
    run_indeterminate_research,
)


def _config(**overrides: Any) -> ScannerConfig:
    return ScannerConfig(
        openai_api_key="secret",
        indeterminate_research_enabled=True,
        **overrides,
    )


def _sourced_researcher(**kwargs: Any) -> dict[str, Any]:
    symbol = kwargs["symbol"]
    return {
        "symbol": symbol,
        "status": "sourced",
        "brief": f"Cited brief for {symbol}.",
        "sources": [{"url": f"https://example.com/{symbol}", "cited": True}],
        "citation_count": 1,
        "source_count": 1,
        "web_search_call_count": 1,
        "market_data_substitute": False,
        "can_create_pick": False,
    }


def test_data_ineligible_run_writes_a_non_actionable_sourced_artifact(tmp_path: Path) -> None:
    out = tmp_path / "indeterminate_research.json"

    result = run_indeterminate_research(
        db_path=tmp_path / "scanner.sqlite",
        symbols=["AAA", "BBB", "AAA"],
        selection_outcome="data_ineligible",
        market_date="2026-08-07",
        out_path=out,
        notify="none",
        config=_config(),
        researcher=_sourced_researcher,
    )

    assert result["status"] == "completed"
    assert result["symbols_researched"] == ["AAA", "BBB"]
    assert result["model_request_count"] == 2
    assert result["citation_count"] == 2
    assert result["market_data_substitute"] is False
    assert result["can_create_pick"] is False
    persisted = json.loads(out.read_text(encoding="utf-8"))
    assert persisted["artifact_hash_sha256"] == result["artifact_hash_sha256"]


def test_non_indeterminate_run_does_not_call_openai(tmp_path: Path) -> None:
    calls = 0

    def researcher(**_: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    result = run_indeterminate_research(
        db_path=tmp_path / "scanner.sqlite",
        symbols=["AAA"],
        selection_outcome="valid_no_edge",
        market_date="2026-08-07",
        out_path=tmp_path / "research.json",
        notify="none",
        config=_config(),
        researcher=researcher,
    )

    assert result["status"] == "skipped_not_indeterminate"
    assert result["model_request_count"] == 0
    assert calls == 0


def test_partial_provider_failure_is_explicit_and_keeps_market_data_missing(
    tmp_path: Path,
) -> None:
    def researcher(**kwargs: Any) -> dict[str, Any]:
        if kwargs["symbol"] == "BBB":
            raise RuntimeError("provider unavailable")
        return _sourced_researcher(**kwargs)

    result = run_indeterminate_research(
        db_path=tmp_path / "scanner.sqlite",
        symbols=["AAA", "BBB", "CCC"],
        selection_outcome="data_ineligible",
        market_date="2026-08-07",
        out_path=tmp_path / "research.json",
        notify="none",
        config=_config(indeterminate_research_max_symbols=2),
        researcher=researcher,
    )

    assert result["status"] == "partial"
    assert result["symbols_deferred"] == ["CCC"]
    assert result["dossiers"][1]["status"] == "provider_error"
    assert result["dossiers"][1]["error_code"] == "RuntimeError"
    assert result["research_summary"]["all_market_data_gaps_remain"] is True
