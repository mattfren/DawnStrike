from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from multiprocessing import get_context
from pathlib import Path

import pytest

from intraday_scanner.public_data import yahoo_chart_fetcher
from intraday_scanner.v2.data.market import MarketBar, MarketDataset, load_ohlcv_csv
from intraday_scanner.v2.data.yahoo_chart import dataset_from_yahoo_chart_payloads


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


def _write_payload_in_child(cache_dir: str, payload: dict[str, object]) -> None:
    yahoo_chart_fetcher._write_immutable_payload(Path(cache_dir), "SPY", payload)


def _rate_gate_in_child(cache_dir: str) -> None:
    gate = yahoo_chart_fetcher._RequestRateGate(1, cache_root=Path(cache_dir))
    gate.wait(None)
    (Path(cache_dir) / f"started_{os.getpid()}.txt").write_text(
        str(time.time()), encoding="ascii"
    )


def test_windows_file_lock_uses_runtime_msvcrt_capabilities(monkeypatch) -> None:
    calls: list[tuple[int, int, int]] = []

    class FakeMsvcrt:
        LK_NBLCK = 7

        @staticmethod
        def locking(file_descriptor: int, mode: int, size: int) -> None:
            calls.append((file_descriptor, mode, size))

    class FakeHandle:
        @staticmethod
        def fileno() -> int:
            return 42

    monkeypatch.setattr(
        yahoo_chart_fetcher.importlib,
        "import_module",
        lambda name: FakeMsvcrt if name == "msvcrt" else None,
    )

    yahoo_chart_fetcher._lock_windows_file(FakeHandle(), "LK_NBLCK")

    assert calls == [(42, 7, 1)]
    with pytest.raises(OSError, match="Windows file locking is unavailable"):
        yahoo_chart_fetcher._lock_windows_file(FakeHandle(), "LK_UNLCK")


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
    assert result.dataset.source_path is not None
    assert Path(result.dataset.source_path).name.startswith("public_yahoo_ohlcv_")
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


def test_yahoo_chart_fetch_rejects_hostile_symbols_before_cache_or_url_use(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        yahoo_chart_fetcher,
        "_fetch_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )
    cache_dir = tmp_path / "cache"
    with pytest.raises(ValueError, match="canonical US market symbols"):
        yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
            symbols=("../ESCAPE",),
            cache_dir=cache_dir,
        )
    assert not cache_dir.exists()


def test_yahoo_chart_fetch_rejects_payload_symbol_identity_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = _payload("WMT")
    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", lambda *_args, **_kwargs: payload)
    result = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY",),
        cache_dir=tmp_path,
        max_attempts=1,
    )
    assert result.dataset.symbols == ()
    assert not tuple(tmp_path.glob("*.json"))
    assert any("identity mismatch" in warning for warning in result.warnings)


def test_yahoo_chart_fetch_binds_governed_class_share_alias_to_canonical_symbol(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_fetch(url: str, *, timeout_seconds: float, **kwargs: object) -> dict[str, object]:
        del timeout_seconds, kwargs
        assert "/BRK-B?" in url
        return _payload("BRK-B")

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", fake_fetch)
    result = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("BRK.B",), cache_dir=tmp_path, max_attempts=1
    )
    assert result.dataset.symbols == ("BRK.B",)
    assert "canonical_symbol:BRK.B" in result.dataset.source_refs
    assert "yahoo_symbol:BRK-B" in result.dataset.source_refs
    assert any("BRK-B?" in reference for reference in result.dataset.source_refs)


def test_yahoo_chart_fetch_rejects_alias_collision_and_wrong_class_share_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        yahoo_chart_fetcher,
        "_fetch_json",
        lambda *_args, **_kwargs: _payload("WMT"),
    )
    with pytest.raises(ValueError, match="alias collision"):
        yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
            symbols=("BRK.B", "BRK-B"), cache_dir=tmp_path
        )
    assert not tuple(tmp_path.iterdir())

    result = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("BRK.B",), cache_dir=tmp_path, max_attempts=1
    )
    assert result.dataset.symbols == ()
    assert not tuple(tmp_path.glob("*.json"))


def test_yahoo_chart_parser_rejects_non_finite_ohlcv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = _payload("SPY")
    payload["chart"]["result"][0]["indicators"]["quote"][0]["open"] = [float("nan")]
    del tmp_path, monkeypatch
    from intraday_scanner.v2.data.yahoo_chart import dataset_from_yahoo_chart_payloads

    result = dataset_from_yahoo_chart_payloads(
        {"SPY": payload}, dataset_id="nan", source_kind="test"
    )
    assert result.dataset_id == "nan"
    assert result.symbols == ()
    assert any("non-finite OHLC" in warning for warning in result.warnings)


def test_yahoo_chart_fetch_uses_bounded_workers_for_large_universe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    symbols = tuple(f"S{index:03d}" for index in range(600))
    calls = 0
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_fetch(url: str, *, timeout_seconds: float, **kwargs: object) -> dict[str, object]:
        del timeout_seconds, kwargs
        nonlocal calls, active, max_active
        with lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
        # Force completion order to differ from requested order.
        time.sleep(0.0005 if int(_symbol_from_url(url)[1:]) % 2 else 0.001)
        with lock:
            active -= 1
        return _payload(_symbol_from_url(url))

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", fake_fetch)
    result = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=symbols,
        cache_dir=tmp_path,
        max_attempts=1,
        max_workers=12,
        max_requests_per_second=None,
    )

    assert calls == len(symbols)
    assert result.dataset.symbols == tuple(sorted(symbols))
    assert 1 <= max_active <= 12
    assert len(result.raw_payload_paths) == len(symbols)


def test_yahoo_chart_fetch_resumes_only_missing_symbols_from_partial_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, int] = {}

    def first_fetch(url: str, *, timeout_seconds: float) -> dict[str, object]:
        del timeout_seconds
        symbol = _symbol_from_url(url)
        calls[symbol] = calls.get(symbol, 0) + 1
        if symbol == "WMT":
            raise OSError("temporary outage")
        return _payload(symbol)

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", first_fetch)
    first = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY", "WMT"),
        cache_dir=tmp_path,
        max_attempts=1,
        retry_backoff_seconds=0,
        required_bar_date=date(2026, 7, 6),
    )
    assert first.dataset.symbols == ("SPY",)
    assert (tmp_path / ".partial" / "2026-07-06").exists()
    assert not (tmp_path / "spy_chart.json").exists()

    def second_fetch(url: str, *, timeout_seconds: float) -> dict[str, object]:
        del timeout_seconds
        symbol = _symbol_from_url(url)
        calls[symbol] = calls.get(symbol, 0) + 1
        return _payload(symbol)

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", second_fetch)
    second = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY", "WMT"),
        cache_dir=tmp_path,
        max_attempts=1,
        retry_backoff_seconds=0,
        required_bar_date=date(2026, 7, 6),
    )
    assert second.dataset.symbols == ("SPY", "WMT")
    assert calls == {"SPY": 1, "WMT": 2}
    assert len(second.raw_payload_paths) == 2


def test_yahoo_chart_fetch_rejects_stale_root_cache_for_required_date(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # The fixture timestamp is deliberately not the required date.  A stale
    # root cache must never satisfy an exact completed-bar request.
    stale = _payload("SPY")
    (tmp_path / "spy_chart.json").write_text(
        yahoo_chart_fetcher.json.dumps(stale, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    calls = 0

    def fake_fetch(url: str, *, timeout_seconds: float) -> dict[str, object]:
        del url, timeout_seconds
        nonlocal calls
        calls += 1
        payload = _payload("SPY")
        payload["chart"]["result"][0]["timestamp"] = [1_787_932_800]
        return payload

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", fake_fetch)
    result = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY",),
        cache_dir=tmp_path,
        max_attempts=1,
        required_bar_date=__import__("datetime").date(2026, 8, 28),
    )
    assert calls == 1
    assert result.dataset.symbols == ("SPY",)
    assert (tmp_path / "spy_chart.json").read_text(encoding="utf-8") != ""
    assert any(path.name.startswith("spy_chart_") for path in result.raw_payload_paths)


def test_yahoo_chart_partial_cache_is_scoped_to_required_date(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout_seconds: float, **kwargs: object) -> dict[str, object]:
        del timeout_seconds, kwargs
        symbol = _symbol_from_url(url)
        calls.append(symbol)
        if symbol == "WMT":
            raise OSError("temporary outage")
        return _payload(symbol)

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", fake_fetch)
    yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY", "WMT"),
        cache_dir=tmp_path,
        max_attempts=1,
        required_bar_date=date(2026, 7, 6),
    )
    calls.clear()
    # The July 6 partial must not satisfy a July 7 request, even though the
    # payload itself is otherwise valid.
    yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY",),
        cache_dir=tmp_path,
        max_attempts=1,
        required_bar_date=date(2026, 7, 7),
    )
    assert calls == ["SPY"]


def test_yahoo_chart_fetch_hash_is_stable_when_completion_order_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    symbols = ("SPY", "IWM", "QQQ", "DIA")
    delays = {"SPY": 0.004, "IWM": 0.001, "QQQ": 0.003, "DIA": 0.0}

    def fake_fetch(url: str, *, timeout_seconds: float) -> dict[str, object]:
        del timeout_seconds
        symbol = _symbol_from_url(url)
        time.sleep(delays[symbol])
        return _payload(symbol)

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", fake_fetch)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=symbols, cache_dir=first_dir, max_attempts=1, max_workers=4
    )
    yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=tuple(reversed(symbols)),
        cache_dir=second_dir,
        max_attempts=1,
        max_workers=4,
    )
    repeat = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=tuple(reversed(symbols)),
        cache_dir=first_dir,
        max_attempts=1,
        max_workers=4,
    )
    first_bytes = (first_dir / "public_yahoo_ohlcv.csv").read_bytes()
    second_bytes = (second_dir / "public_yahoo_ohlcv.csv").read_bytes()
    assert hashlib.sha256(first_bytes).hexdigest() == hashlib.sha256(second_bytes).hexdigest()
    assert repeat.dataset.source_refs == first.dataset.source_refs
    assert repeat.warnings == first.warnings


def test_yahoo_chart_cache_writer_is_concurrency_safe_and_quarantines_corruption(
    tmp_path: Path,
) -> None:
    payload = _payload("SPY")
    content = yahoo_chart_fetcher._canonical_payload_bytes(payload)
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / f"spy_chart_{digest}.json"
    target.write_bytes(b"truncated")

    def write_one() -> Path:
        return yahoo_chart_fetcher._write_immutable_payload(tmp_path, "SPY", payload)

    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = tuple(executor.map(lambda _index: write_one(), range(8)))
    assert all(path == target for path in paths)
    assert target.read_bytes() == content
    assert tuple((tmp_path / ".quarantine").glob("*.corrupt"))


def test_yahoo_chart_raw_sources_are_immutable_across_process_builders(
    tmp_path: Path,
) -> None:
    first_payload = _payload("SPY")
    second_payload = _payload("SPY")
    second_payload["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [202.0]
    expected = {
        hashlib.sha256(yahoo_chart_fetcher._canonical_payload_bytes(payload)).hexdigest():
        yahoo_chart_fetcher._canonical_payload_bytes(payload)
        for payload in (first_payload, second_payload)
    }
    context = get_context("spawn")
    processes = tuple(
        context.Process(target=_write_payload_in_child, args=(str(tmp_path), payload))
        for payload in (first_payload, second_payload)
    )
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        assert process.exitcode == 0
    targets = tuple(tmp_path.glob("spy_chart_*.json"))
    assert {path.stem.rsplit("_", 1)[-1]: path.read_bytes() for path in targets} == expected
    assert not (tmp_path / "spy_chart.json").exists()


def test_yahoo_transport_discards_slow_drip_after_deadline(monkeypatch) -> None:
    class SlowResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, _size: int) -> bytes:
            time.sleep(0.02)
            return b"{}"

    monkeypatch.setattr(
        yahoo_chart_fetcher,
        "open_allowlisted_url",
        lambda *_args, **_kwargs: SlowResponse(),
    )
    with pytest.raises(TimeoutError, match="deadline"):
        yahoo_chart_fetcher._fetch_json(
            "https://query1.finance.yahoo.com/v8/finance/chart/SPY",
            timeout_seconds=1,
            deadline=time.monotonic() + 0.001,
        )


def test_yahoo_rate_gate_is_process_wide_across_invocations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    starts: list[float] = []
    lock = threading.Lock()

    def fake_fetch(url: str, *, timeout_seconds: float, **kwargs: object) -> dict[str, object]:
        del timeout_seconds, kwargs
        with lock:
            starts.append(time.monotonic())
        return _payload(_symbol_from_url(url))

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", fake_fetch)

    def build(symbol: str) -> None:
        yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
            symbols=(symbol,),
            cache_dir=tmp_path / symbol,
            max_attempts=1,
            max_requests_per_second=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(build, ("AAA", "BBB")))
    assert len(starts) == 2
    assert abs(starts[1] - starts[0]) >= 0.9


def test_yahoo_cache_prefers_complete_unique_history_over_one_bar(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rich = _payload("SPY")
    result = rich["chart"]["result"][0]
    base_timestamp = result["timestamp"][0]
    result["timestamp"] = [base_timestamp - 86400, base_timestamp]
    quote = result["indicators"]["quote"][0]
    for key in ("open", "high", "low", "close", "volume"):
        quote[key] = [quote[key][0] - 1, quote[key][0]]
    one_bar = _payload("SPY")
    yahoo_chart_fetcher._write_immutable_payload(tmp_path, "SPY", one_bar)
    yahoo_chart_fetcher._write_immutable_payload(tmp_path, "SPY", rich)
    monkeypatch.setattr(
        yahoo_chart_fetcher,
        "_fetch_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )
    fetched = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY",),
        cache_dir=tmp_path,
        required_bar_date=date.fromtimestamp(base_timestamp),
        minimum_history_bars=2,
        max_attempts=1,
    )
    assert fetched.dataset.symbols == ("SPY",)
    assert len(fetched.dataset.bars_by_symbol["SPY"]) == 2


def test_yahoo_cache_stale_rate_state_recovers_after_restart(tmp_path: Path) -> None:
    lock_path = tmp_path / ".yahoo_rate_gate.lock"
    lock_path.write_text("1337060.0", encoding="ascii")
    yahoo_chart_fetcher._PROCESS_NEXT_REQUEST_AT = 0.0
    started = time.monotonic()
    assert yahoo_chart_fetcher._RequestRateGate(1, cache_root=tmp_path).wait(None)
    assert time.monotonic() - started < 0.5
    lock_path.write_text(str(time.time() + 3600), encoding="ascii")
    yahoo_chart_fetcher._PROCESS_NEXT_REQUEST_AT = 0.0
    started = time.monotonic()
    assert yahoo_chart_fetcher._RequestRateGate(1, cache_root=tmp_path).wait(None)
    assert time.monotonic() - started < 0.5


def test_yahoo_rate_gate_cross_process_invocations_are_paced(tmp_path: Path) -> None:
    context = get_context("spawn")
    processes = tuple(
        context.Process(target=_rate_gate_in_child, args=(str(tmp_path),))
        for _ in range(2)
    )
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        assert process.exitcode == 0
    starts = sorted(
        float(path.read_text(encoding="ascii"))
        for path in tmp_path.glob("started_*.txt")
    )
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.9


@pytest.mark.parametrize(
    ("field", "value"),
    (("open", True), ("volume", None), ("timestamp", True)),
)
def test_yahoo_parser_rejects_bool_and_missing_volume(field: str, value: object) -> None:
    payload = _payload("SPY")
    if field == "timestamp":
        payload["chart"]["result"][0][field] = [value]
    else:
        payload["chart"]["result"][0]["indicators"]["quote"][0][field] = [value]
    parsed = dataset_from_yahoo_chart_payloads(
        {"SPY": payload}, dataset_id="hostile", source_kind="test"
    )
    assert parsed.symbols == ()


def test_yahoo_parser_preserves_large_integer_volume_exactly() -> None:
    payload = _payload("SPY")
    payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"] = [
        9_007_199_254_740_993
    ]
    parsed = dataset_from_yahoo_chart_payloads(
        {"SPY": payload}, dataset_id="large-volume", source_kind="test"
    )
    assert parsed.bars_by_symbol["SPY"][0].volume == 9_007_199_254_740_993


def test_market_csv_preserves_large_integer_volume_and_rejects_nonfinite(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "hostile.csv"
    csv_path.write_text(
        "symbol,timestamp,open,high,low,close,volume\n"
        "SPY,2026-06-01T21:00:00+00:00,100,102,99,101,9007199254740993\n"
        "QQQ,2026-06-01T21:00:00+00:00,NaN,102,99,101,1\n",
        encoding="utf-8",
    )
    dataset = load_ohlcv_csv(
        csv_path, dataset_id="hostile", source_kind="test", timeframe="1d"
    )
    assert dataset.bars_by_symbol["SPY"][0].volume == 9_007_199_254_740_993
    assert "QQQ" not in dataset.bars_by_symbol


def test_market_csv_roundtrip_is_lossless_for_prices_and_vwap(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    source = MarketDataset(
        dataset_id="lossless",
        source_kind="test",
        timeframe="1d",
        bars_by_symbol={
            "SPY": (
                MarketBar(
                    symbol="SPY",
                    timestamp=datetime(2026, 6, 1, 21, tzinfo=timezone.utc),
                    open=100.123456,
                    high=102.234567,
                    low=99.987654,
                    close=101.111119,
                    volume=9_007_199_254_740_993,
                    vwap=100.555555,
                ),
            )
        },
    )
    csv_path = tmp_path / "lossless.csv"
    from intraday_scanner.v2.data.market import write_ohlcv_csv

    write_ohlcv_csv(source, csv_path)
    loaded = load_ohlcv_csv(
        csv_path, dataset_id="lossless", source_kind="test", timeframe="1d"
    )
    assert loaded.bars_by_symbol["SPY"] == source.bars_by_symbol["SPY"]


def test_yahoo_fetch_deadline_returns_without_waiting_for_noncooperative_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        yahoo_chart_fetcher,
        "_fetch_json",
        lambda *_args, **_kwargs: (time.sleep(0.4) or _payload("SPY")),
    )
    started = time.monotonic()
    result = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
        symbols=("SPY",),
        cache_dir=tmp_path,
        max_attempts=1,
        time_budget_seconds=0.03,
    )
    assert time.monotonic() - started < 0.2
    assert result.dataset.symbols == ()
    time.sleep(0.45)
    assert not tuple(tmp_path.glob("*.csv"))
    assert not tuple(tmp_path.glob("*.json"))


def test_yahoo_cache_rejects_wrong_request_contract(tmp_path: Path) -> None:
    payload = _payload("SPY")
    wrong_contract = yahoo_chart_fetcher._request_contract(
        range_period="1mo", interval="1d"
    )
    path = yahoo_chart_fetcher._write_immutable_payload(
        tmp_path,
        "SPY",
        payload,
        request_contract=wrong_contract,
    )
    assert (
        yahoo_chart_fetcher._read_cached_payload(
            tmp_path,
            "SPY",
            provider_symbol="SPY",
            required_bar_date=date.fromtimestamp(1_783_360_800),
            minimum_history_bars=1,
            deadline=None,
            cache_root=tmp_path,
            expected_request_contract=yahoo_chart_fetcher._request_contract(
                range_period="2y", interval="1d"
            ),
        )
        is None
    )
    assert path.is_file()


def test_oversized_raw_and_csv_poison_objects_are_recoverable(tmp_path: Path) -> None:
    payload = _payload("SPY")
    content = yahoo_chart_fetcher._canonical_payload_bytes(payload)
    digest = hashlib.sha256(content).hexdigest()
    raw_path = tmp_path / f"spy_chart_{digest}.json"
    raw_path.write_bytes(b"x" * (yahoo_chart_fetcher._MAX_PAYLOAD_BYTES + 1))
    assert yahoo_chart_fetcher._write_immutable_payload(tmp_path, "SPY", payload) == raw_path
    assert raw_path.read_bytes() == content

    from datetime import datetime, timezone

    dataset = MarketDataset(
        dataset_id="csv-poison",
        source_kind="test",
        timeframe="1d",
        bars_by_symbol={
            "SPY": (
                MarketBar(
                    symbol="SPY",
                    timestamp=datetime(2026, 6, 1, 21, tzinfo=timezone.utc),
                    open=100.0,
                    high=102.0,
                    low=99.0,
                    close=101.0,
                    volume=1,
                ),
            )
        },
    )
    first = yahoo_chart_fetcher._write_csv_immutable(dataset, cache_root=tmp_path)
    csv_content = first.read_bytes()
    first.write_bytes(b"x" * (yahoo_chart_fetcher._MAX_CSV_BYTES + 1))
    repaired = yahoo_chart_fetcher._write_csv_immutable(dataset, cache_root=tmp_path)
    assert repaired == first
    assert repaired.read_bytes() == csv_content


def test_full_universe_csv_over_16mib_is_bounded_and_idempotent(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    bars_by_symbol = {}
    for symbol_index in range(519):
        symbol = f"S{symbol_index:03d}"
        bars_by_symbol[symbol] = tuple(
            MarketBar(
                symbol=symbol,
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc)
                + timedelta(days=bar_index),
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=9_007_199_254_740_993,
            )
            for bar_index in range(510)
        )
    dataset = MarketDataset(
        dataset_id="full-universe",
        source_kind="test",
        timeframe="1d",
        bars_by_symbol=bars_by_symbol,
    )
    first = yahoo_chart_fetcher._write_csv_immutable(dataset, cache_root=tmp_path)
    first_bytes = first.read_bytes()
    assert len(first_bytes) > yahoo_chart_fetcher._MAX_PAYLOAD_BYTES
    second = yahoo_chart_fetcher._write_csv_immutable(dataset, cache_root=tmp_path)
    assert second == first
    assert second.read_bytes() == first_bytes
    loaded = load_ohlcv_csv(
        first, dataset_id="full-universe", source_kind="test", timeframe="1d"
    )
    assert len(loaded.symbols) == 519
    assert loaded.bars_by_symbol["S000"][0].volume == 9_007_199_254_740_993


def test_yahoo_chart_concurrent_builders_return_byte_exact_content_addressed_csv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_fetch(url: str, *, timeout_seconds: float) -> dict[str, object]:
        del timeout_seconds
        symbol = _symbol_from_url(url)
        time.sleep(0.002 if symbol == "AAA" else 0.0)
        return _payload(symbol)

    monkeypatch.setattr(yahoo_chart_fetcher, "_fetch_json", fake_fetch)

    def build(symbol: str) -> bytes:
        result = yahoo_chart_fetcher.fetch_yahoo_chart_daily_dataset(
            symbols=(symbol,), cache_dir=tmp_path, max_attempts=1
        )
        assert result.dataset.source_path is not None
        assert result.dataset.source_refs[0] == result.dataset.source_path
        return Path(result.dataset.source_path).read_bytes()

    with ThreadPoolExecutor(max_workers=2) as executor:
        aaa_bytes, bbb_bytes = tuple(executor.map(build, ("AAA", "BBB")))
    assert b"AAA" in aaa_bytes
    assert b"BBB" in bbb_bytes
    assert aaa_bytes != bbb_bytes
