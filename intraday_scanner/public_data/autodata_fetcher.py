"""Network adapter for optional AutoData provider JSON fetches."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from intraday_scanner.network_safety import open_allowlisted_url


class ProviderHttpError(Exception):
    """HTTP-level provider failure with status code preserved."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderFetchError(Exception):
    """Provider fetch failed before a valid JSON object was available."""


def encode_query(params: dict[str, object]) -> str:
    return urlencode(params)


def fetch_json_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 20.0,
    user_agent: str = "Dawnstrike-v2-AutoData/1.0 research-only",
    allowed_hosts: tuple[str, ...],
) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent,
            **(headers or {}),
        },
    )
    try:
        with open_allowlisted_url(
            request,
            timeout=timeout_seconds,
            allowed_hosts=allowed_hosts,
        ) as response:
            text = response.read().decode("utf-8")
        payload = json.loads(text)
    except HTTPError as exc:
        raise ProviderHttpError(
            f"provider HTTP {exc.code}",
            status_code=exc.code,
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, TypeError) as exc:
        raise ProviderFetchError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise ProviderFetchError("expected JSON object")
    return payload
