from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from intraday_scanner.ai.indeterminate_researcher import research_symbol


class _Response:
    def __init__(self, *, cited: bool) -> None:
        self.output_text = "Issuer filed an 8-K. [citation]"
        self.model = "gpt-test"
        self.id = "resp_test"
        self.status = "completed"
        self.usage = SimpleNamespace(
            model_dump=lambda: {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
        )
        annotations = (
            [
                {
                    "type": "url_citation",
                    "url": "https://www.sec.gov/example#section",
                    "title": "SEC filing",
                }
            ]
            if cited
            else []
        )
        self._payload = {
            "id": self.id,
            "model": self.model,
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "type": "search",
                        "query": "TEST SEC filing",
                        "sources": [
                            {
                                "type": "url",
                                "url": "https://www.sec.gov/example#other",
                                "title": "SEC filing",
                            },
                            {"type": "url", "url": "javascript:alert(1)", "title": "bad"},
                        ],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": self.output_text,
                            "annotations": annotations,
                        }
                    ],
                },
            ],
        }

    def model_dump(self) -> dict[str, Any]:
        return self._payload


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.responses = self

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self.response


def test_web_research_requires_citations_and_persists_tool_sources() -> None:
    client = _Client(_Response(cited=True))

    result = research_symbol(
        symbol="TEST",
        market_date="2026-08-07",
        api_key="secret",  # pragma: allowlist secret
        model="gpt-test",
        timeout_seconds=10,
        max_tool_calls=3,
        client=client,
    )

    assert result["status"] == "sourced"
    assert result["brief"]
    assert result["citation_count"] == 1
    assert result["source_count"] == 1
    assert result["sources"] == [
        {"url": "https://www.sec.gov/example", "title": "SEC filing", "cited": True}
    ]
    assert result["market_data_substitute"] is False
    assert result["can_create_pick"] is False
    assert client.calls[0]["tools"] == [{"type": "web_search"}]
    assert client.calls[0]["include"] == ["web_search_call.action.sources"]
    assert client.calls[0]["reasoning"] == {"effort": "low"}
    assert client.calls[0]["max_output_tokens"] == 4_000
    assert client.calls[0]["store"] is False


def test_uncited_output_is_not_exposed_as_sourced_research() -> None:
    result = research_symbol(
        symbol="TEST",
        market_date="2026-08-07",
        api_key="secret",  # pragma: allowlist secret
        model="gpt-test",
        timeout_seconds=10,
        max_tool_calls=3,
        client=_Client(_Response(cited=False)),
    )

    assert result["status"] == "insufficient_sources"
    assert result["brief"] == ""
    assert result["citation_count"] == 0
    assert result["source_count"] == 1
