"""Fail-closed HTTP transport for Dawnstrike's approved research sources."""

from __future__ import annotations

import urllib.parse
import urllib.request
from collections.abc import Collection
from typing import Any

INTRADAY_MARKET_DATA_HOSTS = ("data.alpaca.markets", "api.polygon.io")
SECRET_ENV_NAMES = (
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "MASSIVE_API_KEY",
    "POLYGON_API_KEY",
)


def require_allowed_network_url(
    url: str,
    *,
    allowed_hosts: Collection[str],
    allow_http: bool = False,
) -> None:
    """Reject non-web, credentialed, or non-allowlisted network targets."""

    parsed = urllib.parse.urlsplit(url)
    permitted_schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme.lower() not in permitted_schemes:
        raise ValueError("network URL must use an approved HTTP scheme")
    if parsed.username or parsed.password:
        raise ValueError("network URL must not contain credentials")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("network URL must include a hostname")
    if not allowed_hosts:
        raise ValueError("network URL requires an explicit host allowlist")
    if not any(_host_matches(host, item) for item in allowed_hosts):
        raise ValueError("network URL host is not allowlisted")
    if parsed.port is not None:
        expected_port = 80 if parsed.scheme.lower() == "http" else 443
        if parsed.port != expected_port:
            raise ValueError("network URL uses a non-default port")


def open_allowlisted_url(
    target: str | urllib.request.Request,
    *,
    timeout: float,
    allowed_hosts: Collection[str],
    allow_http: bool = False,
) -> Any:
    """Open an approved URL and revalidate every HTTP redirect target."""

    require_allowed_network_url(
        _target_url(target), allowed_hosts=allowed_hosts, allow_http=allow_http
    )
    opener = urllib.request.build_opener(
        _AllowlistedRedirectHandler(
            allowed_hosts=allowed_hosts,
            allow_http=allow_http,
        )
    )
    return opener.open(target, timeout=timeout)  # nosec B310


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allowed_hosts: Collection[str], allow_http: bool) -> None:
        super().__init__()
        self._allowed_hosts = tuple(allowed_hosts)
        self._allow_http = allow_http

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        require_allowed_network_url(
            newurl,
            allowed_hosts=self._allowed_hosts,
            allow_http=self._allow_http,
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _target_url(target: str | urllib.request.Request) -> str:
    return target.full_url if isinstance(target, urllib.request.Request) else target


def _host_matches(host: str, allowed: str) -> bool:
    candidate = str(allowed).strip().lower().rstrip(".")
    if not candidate or "/" in candidate or ":" in candidate:
        return False
    return host == candidate or host.endswith(f".{candidate}")


def assert_secret_not_in_text(text: str, secrets: Collection[str]) -> None:
    """Fail closed if a receipt/log candidate contains a non-empty secret."""

    for secret in secrets:
        if secret and secret in text:
            raise ValueError("secret material is not permitted in evidence text")
