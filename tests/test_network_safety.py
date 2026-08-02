from __future__ import annotations

import pytest

from intraday_scanner.network_safety import require_allowed_network_url


def test_network_url_requires_https_allowlist_and_default_port() -> None:
    require_allowed_network_url(
        "https://api.example.com/v1/data",
        allowed_hosts=("example.com",),
    )


@pytest.mark.parametrize(
    "url",
    (
        "file:///C:/secret.txt",
        "https://user:pass@example.com/data",  # pragma: allowlist secret - rejection fixture
        "https://evil-example.com/data",
        "https://example.com:444/data",
        "http://example.com/data",
    ),
)
def test_network_url_rejects_unsafe_or_unallowlisted_targets(url: str) -> None:
    with pytest.raises(ValueError, match="network URL"):
        require_allowed_network_url(url, allowed_hosts=("example.com",))


def test_network_url_can_explicitly_allow_http_for_legacy_web_sources() -> None:
    require_allowed_network_url(
        "http://sub.example.com/data",
        allowed_hosts=("example.com",),
        allow_http=True,
    )
