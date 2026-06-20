"""Provider health recording helpers."""

from __future__ import annotations

from collections.abc import Callable

from intraday_scanner.models import utc_now_iso


def record_health_check(
    store: object,
    *,
    provider: str,
    check: Callable[[], None],
) -> None:
    """Run a provider check and persist sanitized health state."""

    try:
        check()
    except Exception as exc:
        _record(store, provider, "error", _sanitize(str(exc)))
        raise
    _record(store, provider, "ok", "ready")


def record_health_status(
    store: object,
    *,
    provider: str,
    status: str,
    detail: str,
) -> None:
    _record(store, provider, status, _sanitize(detail))


def _record(store: object, provider: str, status: str, detail: str) -> None:
    recorder = getattr(store, "record_provider_health", None)
    if callable(recorder):
        recorder(provider, status, utc_now_iso(), detail)


def _sanitize(detail: str) -> str:
    redacted = detail
    for marker in ("key=", "token=", "secret=", "password="):
        if marker in redacted.lower():
            return "provider health check failed; sensitive detail redacted"
    return redacted[:500]
