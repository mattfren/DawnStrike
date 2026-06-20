import pytest

from intraday_scanner.errors import DataProviderError
from intraday_scanner.services.provider_health_service import record_health_check
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_provider_health_records_success(tmp_path):
    store = SQLiteScanStore(tmp_path / "scanner.sqlite")

    record_health_check(store, provider="csv", check=lambda: None)

    rows = store.load_provider_health()
    assert rows[0]["provider"] == "csv"
    assert rows[0]["status"] == "ok"
    assert rows[0]["detail"] == "ready"


def test_provider_health_records_sanitized_failure(tmp_path):
    store = SQLiteScanStore(tmp_path / "scanner.sqlite")

    with pytest.raises(DataProviderError):
        record_health_check(
            store,
            provider="newsapi",
            check=lambda: (_ for _ in ()).throw(
                DataProviderError("request failed key=abc123 secret=hidden")
            ),
        )

    rows = store.load_provider_health()
    assert rows[0]["provider"] == "newsapi"
    assert rows[0]["status"] == "error"
    assert rows[0]["detail"] == "provider health check failed; sensitive detail redacted"
    assert "abc123" not in rows[0]["detail"]
