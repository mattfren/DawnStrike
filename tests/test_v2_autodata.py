from __future__ import annotations

import ast
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from intraday_scanner.public_data.autodata_fetcher import ProviderHttpError

from intraday_scanner.v2.autodata import (
    build,
    feed_filltruth,
    fetch,
    fetch_pending,
    providers,
    readiness,
    reconcile,
)
from intraday_scanner.v2.autodata import core as autodata_core
from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.command_center.builder import REQUIRED_PAGES
from intraday_scanner.v2.data import MarketBar, MarketDataset, load_ohlcv_csv, write_ohlcv_csv
from intraday_scanner.v2.evidence_commit import core as evidence_commit_core
from intraday_scanner.v2.omega_sentinel import __main__ as sentinel_cli

RUN_DATE = date(2026, 6, 29)
REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_ENV_VARS = (
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "ALPACA_DATA_FEED",
    "ALPHA_VANTAGE_API_KEY",
    "TWELVE_DATA_API_KEY",
)


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _artifact_text(root: Path) -> str:
    chunks: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".json", ".md", ".csv", ".html"}:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def _clear_provider_env(monkeypatch) -> None:
    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _seed_pending_order() -> None:
    _write_json(
        Path("data/v2_paper_ops/state/pending_orders.json"),
        [
            {
                "direction": "long",
                "earliest_fill_date": RUN_DATE.isoformat(),
                "entry": 100.0,
                "mode": "forward",
                "order_id": "order:forward:2026-06-29:fixture_strategy:v1:QQQ",
                "order_status": "pending",
                "pick_id": "pick:forward:2026-06-29:fixture_strategy:v1:QQQ",
                "quantity": 10,
                "risk_per_unit": 5.0,
                "signal_time": "2026-06-26T20:00:00+00:00",
                "stop": 95.0,
                "strategy_id": "fixture_strategy",
                "strategy_version": "v1",
                "symbol": "QQQ",
                "target": 110.0,
            }
        ],
    )
    _write_json(Path("data/v2_paper_ops/state/open_positions.json"), [])


def _seed_provider_manifest(
    *,
    provider_id: str,
    source_label: str,
    bars: list[MarketBar],
) -> None:
    normalized_path = (
        Path("data/v2_autodata/provider_seed")
        / provider_id
        / "QQQ"
        / f"{RUN_DATE.isoformat()}.csv"
    )
    write_ohlcv_csv(
        MarketDataset(
            dataset_id=f"fixture_{provider_id}",
            source_kind=source_label,
            timeframe="1min",
            bars_by_symbol={"QQQ": tuple(bars)},
        ),
        normalized_path,
    )
    _write_json(
        Path("data/v2_autodata/manifests")
        / f"{RUN_DATE.isoformat()}_{provider_id}_request.json",
        {
            "accepted_bar_count": len(bars),
            "interval": "1min",
            "normalized_artifact_path": normalized_path.as_posix(),
            "provider_id": provider_id,
            "raw_artifact_path": f"data/v2_autodata/raw/{provider_id}/QQQ/raw.json",
            "raw_artifact_sha256": f"{provider_id}-raw-hash",
            "request_id": f"fixture_{provider_id}",
            "source_label": source_label,
            "source_trust_level": source_label,
            "status": "passed",
            "symbol": "QQQ",
            "trade_date": RUN_DATE.isoformat(),
            "warnings": [],
        },
    )


def test_provider_registry_readiness_lists_keys_without_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)

    registry = providers(output_root=Path("data/v2_autodata"))
    ready = readiness(output_root=Path("data/v2_autodata"))
    report = _read_json(Path("data/v2_autodata/reports/provider_readiness.json"))

    assert registry["provider_count"] == 5
    assert registry["enabled_count"] == 1
    assert ready["status"] == "ready_public_fallback_only"
    assert ready["public_fallback_available"] is True
    assert ready["exact_env_vars_to_set_next"] == [
        "ALPACA_API_KEY_ID",
        "ALPACA_API_SECRET_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "TWELVE_DATA_API_KEY",
    ]
    provider_ids = {row["provider_id"] for row in report["providers"]}
    assert provider_ids == {
        "alpaca_market_data",
        "alpha_vantage",
        "twelve_data",
        "yahoo_chart_public_fallback",
        "mock_provider_for_tests",
    }
    assert "public fallback is single-provider" in "\n".join(report["warnings"])


def test_mock_provider_fetch_writes_raw_cache_hash_and_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)

    result = fetch(
        symbol="qqq",
        run_date=RUN_DATE,
        provider_id="mock_provider_for_tests",
        output_root=Path("data/v2_autodata"),
    )
    raw_path = Path(str(result["raw_artifact_path"]))
    manifest = _read_json(Path("data/v2_autodata/manifests/latest_request.json"))

    assert result["symbol"] == "QQQ"
    assert result["accepted_bar_count"] == 3
    assert result["source_label"] == "mock_test_intraday"
    assert result["status"] in {"passed", "passed_with_warnings"}
    assert raw_path.exists()
    assert result["raw_artifact_sha256"] == hashlib.sha256(raw_path.read_bytes()).hexdigest()
    assert manifest["request_id"] == result["request_id"]
    assert Path(str(result["normalized_artifact_path"])).exists()
    assert "mock provider is tests/demo only" in "\n".join(result["warnings"])

    reused = fetch(
        symbol="QQQ",
        run_date=RUN_DATE,
        provider_id="mock_provider_for_tests",
        output_root=Path("data/v2_autodata"),
    )
    assert reused["request_id"] == result["request_id"]
    assert reused["cache_status"] == "reused"


def test_provider_auth_failure_is_cached_without_secret_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)
    fake_secret = "alpha-secret-value-should-not-leak"
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", fake_secret)

    def raise_auth_error(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ProviderHttpError("auth", status_code=403)

    monkeypatch.setattr(autodata_core, "_http_json", raise_auth_error)

    result = fetch(
        symbol="QQQ",
        run_date=RUN_DATE,
        provider_id="alpha_vantage",
        output_root=Path("data/v2_autodata"),
    )
    artifacts = _artifact_text(Path("data/v2_autodata"))

    assert result["status"] == "provider_auth_failed"
    assert result["accepted_bar_count"] == 0
    assert "ALPHA_VANTAGE_API_KEY" in artifacts
    assert fake_secret not in artifacts


def test_provider_payload_normalizers_accept_supported_shapes() -> None:
    timestamp = datetime(2026, 6, 29, 13, 30, tzinfo=timezone.utc)
    provider_payloads: dict[str, dict[str, object]] = {
        "alpaca_market_data": {
            "bars": {
                "QQQ": [
                    {
                        "t": timestamp.isoformat(),
                        "o": 100.0,
                        "h": 101.0,
                        "l": 99.5,
                        "c": 100.5,
                        "v": 1234,
                    }
                ]
            }
        },
        "alpha_vantage": {
            "Time Series (1min)": {
                timestamp.isoformat(): {
                    "1. open": "100.0",
                    "2. high": "101.0",
                    "3. low": "99.5",
                    "4. close": "100.5",
                    "5. volume": "1234",
                }
            }
        },
        "twelve_data": {
            "values": [
                {
                    "datetime": timestamp.isoformat(),
                    "open": "100.0",
                    "high": "101.0",
                    "low": "99.5",
                    "close": "100.5",
                    "volume": "1234",
                }
            ]
        },
        "yahoo_chart_public_fallback": {
            "chart": {
                "error": None,
                "result": [
                    {
                        "timestamp": [int(timestamp.timestamp())],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100.0],
                                    "high": [101.0],
                                    "low": [99.5],
                                    "close": [100.5],
                                    "volume": [1234],
                                }
                            ]
                        },
                    }
                ],
            }
        },
    }

    for provider_id, payload in provider_payloads.items():
        bars, warnings = autodata_core._normalize_provider_payload(
            autodata_core._provider_definition(provider_id),
            "QQQ",
            payload,
            "1min",
        )

        assert warnings == []
        assert len(bars) == 1
        assert bars[0].symbol == "QQQ"
        assert bars[0].timestamp == timestamp
        assert bars[0].close == 100.5


def test_pending_order_fetch_build_and_reconcile_keep_mock_out_of_operational_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(autodata_core, "_write_docs", lambda: None)
    _seed_pending_order()

    pending = fetch_pending(
        run_date=RUN_DATE,
        provider_id="mock_provider_for_tests",
        output_root=Path("data/v2_autodata"),
    )
    operational_build = build(run_date=RUN_DATE, output_root=Path("data/v2_autodata"))
    demo_build = build(
        run_date=RUN_DATE,
        include_demo=True,
        output_root=Path("data/v2_autodata"),
    )
    reconciliation = reconcile(run_date=RUN_DATE, output_root=Path("data/v2_autodata"))

    assert pending["status"] == "passed"
    assert pending["fetched_count"] == 1
    assert operational_build["accepted_bar_count"] == 0
    assert operational_build["status"] == "blocked_needs_autodata_provider"
    assert demo_build["accepted_bar_count"] == 3
    assert demo_build["source_label"] == "mock_test_intraday"
    assert reconciliation["reconciliation_status"] == "single_provider_unreconciled"


def test_build_selects_broker_canonical_without_merging_public_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)
    alpaca_bars = [
        MarketBar(
            "QQQ",
            datetime(2026, 6, 29, 13, 30, tzinfo=timezone.utc),
            100.0,
            100.8,
            99.8,
            100.4,
            1000,
        ),
        MarketBar(
            "QQQ",
            datetime(2026, 6, 29, 13, 31, tzinfo=timezone.utc),
            100.4,
            101.0,
            100.1,
            100.9,
            1100,
        ),
    ]
    yahoo_bars = [
        MarketBar(
            "QQQ",
            datetime(2026, 6, 29, 13, 29, tzinfo=timezone.utc),
            99.7,
            100.0,
            99.6,
            99.9,
            500,
        ),
        MarketBar(
            "QQQ",
            datetime(2026, 6, 29, 13, 30, tzinfo=timezone.utc),
            100.0,
            100.8,
            99.8,
            100.4,
            1000,
        ),
        MarketBar(
            "QQQ",
            datetime(2026, 6, 29, 13, 31, tzinfo=timezone.utc),
            100.4,
            101.0,
            100.1,
            100.9,
            1100,
        ),
    ]
    _seed_provider_manifest(
        provider_id="alpaca_market_data",
        source_label="broker_or_vendor_intraday",
        bars=alpaca_bars,
    )
    _seed_provider_manifest(
        provider_id="yahoo_chart_public_fallback",
        source_label="public_intraday_single_provider",
        bars=yahoo_bars,
    )

    build_payload = build(run_date=RUN_DATE, output_root=Path("data/v2_autodata"))
    reconciliation = reconcile(run_date=RUN_DATE, output_root=Path("data/v2_autodata"))
    canonical_selection = _read_json(
        Path("data/v2_autodata/reports/canonical_selection_latest.json")
    )
    canonical_dataset = load_ohlcv_csv(
        Path(str(build_payload["canonical_artifact_path"])),
        dataset_id="canonical",
        source_kind="broker_or_vendor_intraday",
        timeframe="1min",
    )
    compatibility_dataset = load_ohlcv_csv(
        Path("data/v2_autodata/normalized/latest_provider_intraday.csv"),
        dataset_id="compatibility",
        source_kind="broker_or_vendor_intraday",
        timeframe="1min",
    )

    assert build_payload["canonical_provider_id"] == "alpaca_market_data"
    assert build_payload["comparison_provider_ids"] == ["yahoo_chart_public_fallback"]
    assert build_payload["accepted_bar_count"] == len(alpaca_bars)
    assert build_payload["canonical_duplicate_timestamp_count"] == 0
    assert canonical_selection["status"] == "passed"
    assert canonical_dataset.total_bars == len(alpaca_bars)
    assert compatibility_dataset.total_bars == len(alpaca_bars)
    assert Path(
        "data/v2_autodata/normalized/per_provider/alpaca_market_data/QQQ/2026-06-29.csv"
    ).exists()
    assert Path(
        "data/v2_autodata/normalized/per_provider/yahoo_chart_public_fallback/QQQ/2026-06-29.csv"
    ).exists()
    assert Path(
        "data/v2_autodata/normalized/canonical/QQQ/2026-06-29_canonical_intraday.csv"
    ).exists()
    assert reconciliation["reconciliation_status"] == "provider_with_public_fallback_comparison"
    assert reconciliation["canonical_duplicate_timestamp_count"] == 0
    assert reconciliation["diff_artifact_paths"]
    assert Path(str(reconciliation["diff_artifact_paths"][0])).exists()


def test_feed_filltruth_blocks_cleanly_without_provider_bars(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)

    result = feed_filltruth(run_date=RUN_DATE, output_root=Path("data/v2_autodata"))

    assert result["status"] == "blocked_needs_autodata_provider"
    assert result["accepted_bar_count"] == 0
    assert result["filltruth_status"] == "skipped_no_provider_data"
    assert Path("data/v2_autodata/reports/autodata_filltruth_latest.json").exists()


def test_commitbridge_provider_requirement_contract_distinguishes_public_fallback() -> None:
    assert evidence_commit_core.EvidenceCommitSource.PROVIDER_INTRADAY.value == "provider_intraday"
    assert (
        evidence_commit_core.EvidenceCommitSource.BROKER_OR_VENDOR_INTRADAY.value
        == "broker_or_vendor_intraday"
    )
    assert (
        evidence_commit_core.EvidenceCommitSource.PUBLIC_INTRADAY_SINGLE_PROVIDER.value
        == "public_intraday_single_provider"
    )
    assert evidence_commit_core._proposal_has_provider_intraday(
        {
            "source_kind": "provider_intraday",
            "canonical_dataset_hash": "canonical-hash",
            "canonical_duplicate_timestamp_count": 0,
            "canonical_provider_id": "alpaca_market_data",
            "provider_reconciliation_status": "reconciled_with_minor_diffs",
            "source_file_sha256": "raw-hash",
            "real_intraday_reconciliation_status": "reconciled_with_minor_diffs",
        }
    )
    assert not evidence_commit_core._proposal_has_provider_intraday(
        {
            "source_kind": "public_intraday_single_provider",
            "source_file_sha256": "raw-hash",
            "real_intraday_reconciliation_status": "reconciled_with_minor_diffs",
        }
    )


def test_sentinel_cli_passes_autodata_flag(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_morning_check(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "passed", "autodata": kwargs["autodata"]}

    monkeypatch.setattr(sentinel_cli, "morning_check", fake_morning_check)

    assert sentinel_cli.main(["morning-check", "--date", RUN_DATE.isoformat(), "--autodata"]) == 0
    assert captured["run_date"] == RUN_DATE
    assert captured["autodata"] is True


def test_command_center_autodata_pages_render_without_scripts_or_secret_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "command-center-secret")
    providers(output_root=Path("data/v2_autodata"))

    center = build_command_center()
    autodata_pages = (
        "autodata.html",
        "provider_readiness.html",
        "provider_fetches.html",
        "provider_reconciliation.html",
        "autodata_pending_orders.html",
        "autodata_filltruth.html",
    )

    assert center.status == "passed"
    assert set(autodata_pages).issubset(set(REQUIRED_PAGES))
    for page in autodata_pages:
        text = Path("data/v2_command_center", page).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "research-only; no live execution." in lowered
        assert "<script" not in lowered
        assert "command-center-secret" not in text


def test_autodata_core_stays_read_only_and_network_isolated() -> None:
    forbidden_import_roots = {
        "app",
        "httpx",
        "requests",
        "socket",
        "sqlite3",
        "streamlit",
        "urllib",
    }
    forbidden_import_prefixes = {
        "intraday_scanner.integrations.brokers",
        "intraday_scanner.storage",
    }
    forbidden_calls = {
        "connect",
        "execute",
        "executemany",
        "submit" + "_order",
        "place" + "_order",
        "create" + "_order",
    }

    for path in (REPO_ROOT / "intraday_scanner/v2/autodata").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_import_roots, path
                    assert not any(
                        alias.name.startswith(prefix) for prefix in forbidden_import_prefixes
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_import_roots, path
                assert not any(
                    node.module.startswith(prefix) for prefix in forbidden_import_prefixes
                )
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else ""
                if isinstance(func, ast.Name):
                    name = func.id
                assert name not in forbidden_calls, path

    test_tree = ast.parse((REPO_ROOT / "tests/test_v2_autodata.py").read_text(encoding="utf-8"))
    for node in ast.walk(test_tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert not {"httpx", "requests", "socket", "urllib"} & imported
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in {"httpx", "requests", "socket", "urllib"}
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else ""
            if isinstance(func, ast.Name):
                name = func.id
            assert name not in {"urlopen", "fetch_json_url"}
