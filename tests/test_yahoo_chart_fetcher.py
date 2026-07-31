from __future__ import annotations

from pathlib import Path

from intraday_scanner.public_data import yahoo_chart_fetcher


def _payload(symbol: str) -> dict[str, object]:
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {"symbol": symbol},
                    "timestamp": [1_783_360_800],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0],
                                "high": [102.0],
                                "low": [99.0],
                                "close": [101.0],
                                "volume": [1_000_000],
                            }
                        ]
                    },
                }
            ],
        }
    }


def _symbol_from_url(url: str) -> str:
    return url.split("/chart/", 1)[1].split("?", 1)[0]


def test_yahoo_chart_fetch_retries_transient_symbol_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attempts: dict[str, int] = {}

    def fake_fetch(url: str, *, timeout_seconds: float) -> dict[str, object]:
        del timeout_seconds
        symbol = _symbol_from_url(url)
        attempts[symbol] = attempts.get(symbol, 0) + 1
        if symbol == "IWM" and attempts[symbol] < 3:
            raise OSError("transient HTTP 400")
        return _payload(symbol)

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", fake_fetch)

    result = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY", "IWM"),
        cache_dir=tmp_path,
        max_attempts=3,
        retry_backoff_seconds=0,
    )

    assert result.dataset.symbols == ("IWM", "SPY")
    assert result.dataset.source_path == (tmp_path / "public_yahoo_ohlcv.csv").as_posix()
    assert attempts == {"SPY": 1, "IWM": 3}
    assert len(result.raw_payload_paths) == 2
    assert "IWM: public chart fetch succeeded after 3 attempts" in result.warnings


def test_incomplete_yahoo_fetch_does_not_replace_complete_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cached_csv = tmp_path / "public_yahoo_ohlcv.csv"
    cached_csv.parent.mkdir(parents=True, exist_ok=True)
    cached_csv.write_text("retained-complete-cache\n", encoding="utf-8")
    attempts: dict[str, int] = {}

    def fake_fetch(url: str, *, timeout_seconds: float) -> dict[str, object]:
        del timeout_seconds
        symbol = _symbol_from_url(url)
        attempts[symbol] = attempts.get(symbol, 0) + 1
        if symbol == "WMT":
            raise OSError("persistent HTTP 400")
        return _payload(symbol)

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", fake_fetch)

    result = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY", "WMT"),
        cache_dir=tmp_path,
        max_attempts=3,
        retry_backoff_seconds=0,
    )

    assert result.dataset.symbols == ("SPY",)
    assert result.dataset.source_path is None
    assert result.raw_payload_paths == ()
    assert attempts == {"SPY": 1, "WMT": 3}
    assert cached_csv.read_text(encoding="utf-8") == "retained-complete-cache\n"
    assert not (tmp_path / "spy_chart.json").exists()
    assert any("incomplete requested symbol set" in item for item in result.warnings)


def test_yahoo_chart_fetch_rejects_invalid_retry_configuration(tmp_path: Path) -> None:
    for max_attempts, retry_backoff_seconds, message in (
        (0, 0.0, "max_attempts"),
        (1, -0.1, "retry_backoff_seconds"),
    ):
        try:
            yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
                symbols=("SPY",),
                cache_dir=tmp_path,
                max_attempts=max_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
            )
        except ValueError as exc:
            assert message in str(exc)
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("invalid retry configuration was accepted")
