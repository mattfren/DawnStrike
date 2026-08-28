from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from multiprocessing import get_context
from pathlib import Path

import pytest

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


def _write_payload_in_child(cache_dir: str, payload: dict[str, object]) -> None:
    yahoo_chart_fetcher._write_immutable_payload(Path(cache_dir), "SPY", payload)


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
    digest = hashlib.sha256(content).hexdigest()[:16]
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
        hashlib.sha256(yahoo_chart_fetcher._canonical_payload_bytes(payload)).hexdigest()[:16]:
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
