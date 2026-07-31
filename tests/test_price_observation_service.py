from __future__ import annotations

import csv
from pathlib import Path

import intraday_scanner.services.price_observation_service as price_service
from intraday_scanner.cli import main
from intraday_scanner.dashboard.data_loader import build_operator_today_model
from intraday_scanner.errors import DataProviderError
from intraday_scanner.services.price_observation_service import collect_price_observations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_csv_price_observation_persists_latest_prior_bar_without_lookahead(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    minute_bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [
            {
                "ticker": "NOVA",
                "timestamp": "2026-06-22T09:30:00-04:00",
                "open": "10",
                "high": "10.2",
                "low": "9.9",
                "close": "10",
                "volume": "1000",
            },
            {
                "ticker": "NOVA",
                "timestamp": "2026-06-22T09:35:00-04:00",
                "open": "99",
                "high": "99",
                "low": "99",
                "close": "99",
                "volume": "1000",
            },
        ],
    )
    store = SQLiteScanStore(db_path)
    _persist_signal(store, ticker="NOVA")

    result = collect_price_observations(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="2026-06-22T09:34:00-04:00",
        minute_bars=minute_bars,
        max_age_seconds=600,
    )

    observations = store.load_price_observations(usable_only=True)
    row = observations[0]
    assert result["usable_count"] == 1
    assert row["ticker"] == "NOVA"
    assert row["price"] == 10.0
    assert row["provider_status"] == "fresh_prior_bar"
    assert row["freshness_seconds"] == 240
    assert row["no_lookahead"] is True


def test_csv_price_observation_rejects_stale_prior_bar(tmp_path: Path) -> None:
    db_path = tmp_path / "scanner.sqlite"
    minute_bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [
            {
                "ticker": "NOVA",
                "timestamp": "2026-06-22T09:30:00-04:00",
                "open": "10",
                "high": "10.2",
                "low": "9.9",
                "close": "10",
                "volume": "1000",
            }
        ],
    )

    result = collect_price_observations(
        db_path=db_path,
        source="csv",
        tickers=["NOVA"],
        market_date="2026-06-22",
        requested_at="2026-06-22T09:40:00-04:00",
        minute_bars=minute_bars,
        max_age_seconds=120,
    )

    row = SQLiteScanStore(db_path).load_price_observations()[0]
    assert result["status"] == "no_usable_prices"
    assert result["usable_count"] == 0
    assert row["price"] is None
    assert row["provider_status"] == "stale_rejected"
    assert row["is_usable"] == 0


def test_cli_price_observe_csv_updates_dashboard_current_price(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    minute_bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [
            {
                "ticker": "NOVA",
                "timestamp": "2026-06-22T09:35:00-04:00",
                "open": "10.45",
                "high": "10.8",
                "low": "10.25",
                "close": "10.5",
                "volume": "1000",
            }
        ],
    )
    store = SQLiteScanStore(db_path)
    _persist_signal(store, ticker="NOVA")

    status = main(
        [
            "price-observe",
            "--source",
            "csv",
            "--db-path",
            str(db_path),
            "--minute-bars",
            str(minute_bars),
            "--market-date",
            "2026-06-22",
            "--at",
            "09:35",
        ]
    )

    captured = capsys.readouterr()
    observations = store.load_price_observations(usable_only=True)
    model = build_operator_today_model(
        {
            "historical_signals": store.load_historical_signals(market_date="2026-06-22"),
            "price_observations": observations,
        }
    )
    row = model["watchlist"][0]
    assert status == 0
    assert '"usable_count": 1' in captured.out
    assert observations[0]["price"] == 10.5
    assert row["Current"] == "$10.5"
    assert row["_current_source"] == "price_observations"


def test_yahoo_price_observation_persists_public_chart_price(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    _persist_signal(SQLiteScanStore(db_path), ticker="NOVA")

    monkeypatch.setattr(
        price_service,
        "_fetch_yahoo_chart",
        lambda symbol, config: {
            "chart": {
                "result": [
                    {
                        "timestamp": [1782135000, 1782135300],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [10.0, 10.5],
                                    "high": [10.4, 10.9],
                                    "low": [9.9, 10.4],
                                    "close": [10.25, 10.75],
                                    "volume": [1000, 1500],
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        },
    )

    result = collect_price_observations(
        db_path=db_path,
        source="yahoo",
        market_date="2026-06-22",
        requested_at="2026-06-22T09:35:00-04:00",
        max_age_seconds=600,
    )

    row = SQLiteScanStore(db_path).load_price_observations(usable_only=True)[0]
    assert result["usable_count"] == 1
    assert row["ticker"] == "NOVA"
    assert row["price"] == 10.75
    assert row["source"] == "yahoo"
    assert row["source_kind"] == "public_web_market_data"
    assert row["provider"] == "yahoo_finance_chart"


def test_auto_price_observe_uses_yahoo_when_no_minute_file(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    monkeypatch.setattr(
        price_service,
        "_fetch_yahoo_chart",
        lambda symbol, config: {
            "chart": {
                "result": [
                    {
                        "timestamp": [1782135000],
                        "indicators": {"quote": [{"close": [10.25]}]},
                    }
                ]
            }
        },
    )

    status = main(
        [
            "price-observe",
            "--tickers",
            "NOVA",
            "--db-path",
            str(db_path),
            "--market-date",
            "2026-06-22",
            "--at",
            "09:30",
        ]
    )

    captured = capsys.readouterr()
    row = SQLiteScanStore(db_path).load_price_observations(usable_only=True)[0]
    assert status == 0
    assert '"source": "yahoo"' in captured.out
    assert row["price"] == 10.25


def test_yahoo_provider_error_persists_rejected_observation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "scanner.sqlite"

    def fail(symbol, config):
        raise DataProviderError("Yahoo Finance price request failed with HTTP 404")

    monkeypatch.setattr(price_service, "_fetch_yahoo_chart", fail)

    result = collect_price_observations(
        db_path=db_path,
        source="yahoo",
        tickers=["FAKE"],
        market_date="2026-06-22",
        requested_at="09:30",
    )

    row = SQLiteScanStore(db_path).load_price_observations()[0]
    assert result["status"] == "no_usable_prices"
    assert row["ticker"] == "FAKE"
    assert row["price"] is None
    assert row["provider_status"] == "provider_error"
    assert row["is_usable"] == 0


def test_price_observation_service_persists_when_store_method_is_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delattr(SQLiteScanStore, "persist_price_observations", raising=False)
    db_path = tmp_path / "scanner.sqlite"
    minute_bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [
            {
                "ticker": "NOVA",
                "timestamp": "2026-06-22T09:30:00-04:00",
                "open": "10",
                "high": "10.2",
                "low": "9.9",
                "close": "10",
                "volume": "1000",
            }
        ],
    )

    result = collect_price_observations(
        db_path=db_path,
        source="csv",
        tickers=["NOVA"],
        market_date="2026-06-22",
        requested_at="09:30",
        minute_bars=minute_bars,
    )

    row = SQLiteScanStore(db_path).load_price_observations(usable_only=True)[0]
    assert result["persisted"]["inserted"] == 1
    assert row["ticker"] == "NOVA"
    assert row["price"] == 10.0


def test_saved_signal_targets_skip_no_trade_and_dedupe_yahoo_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_signal(store, ticker="NOVA")
    _persist_signal(store, ticker="NOVA")
    _persist_signal(store, ticker="NO_TRADE")
    calls: list[str] = []

    def fake_fetch(symbol, config):
        calls.append(symbol)
        return {
            "chart": {
                "result": [
                    {
                        "timestamp": [1782135000],
                        "indicators": {"quote": [{"close": [10.25]}]},
                    }
                ]
            }
        }

    monkeypatch.setattr(price_service, "_fetch_yahoo_chart", fake_fetch)

    result = collect_price_observations(
        db_path=db_path,
        source="yahoo",
        market_date="2026-06-22",
        requested_at="09:30",
    )

    assert calls == ["NOVA"]
    assert result["target_count"] == 1
    assert result["usable_count"] == 1


def test_cli_price_observe_alpaca_missing_secrets_fails_without_leaking_values(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "secret-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "")

    status = main(
        [
            "price-observe",
            "--source",
            "alpaca",
            "--tickers",
            "NOVA",
            "--db-path",
            str(tmp_path / "scanner.sqlite"),
            "--market-date",
            "2026-06-22",
            "--at",
            "09:35",
        ]
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert status == 1
    assert "Missing Alpaca market-data credential" in captured.err
    assert "ALPACA_API_SECRET_KEY" in captured.err
    assert "secret-key-id" not in combined


def _write_minute_bars(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["ticker", "timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def _persist_signal(store: SQLiteScanStore, *, ticker: str) -> None:
    store.persist_historical_signals(
        [
            {
                "signal_id": f"sig-{ticker}",
                "scan_id": "scan-1",
                "generated_at": "2026-06-22T13:20:00+00:00",
                "market_date": "2026-06-22",
                "ticker": ticker,
                "rank": 1,
                "source": "test",
                "source_confidence": 90,
                "primary_setup": "Momentum",
                "setup_grade": "A",
                "signal_label": "WATCH",
                "entry_watch_level": 10.25,
                "invalidation_level": 9.5,
                "target_1": 11.0,
                "raw_payload_json": {},
            }
        ]
    )
