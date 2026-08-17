from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

import app as dashboard_app
from intraday_scanner import cli
from intraday_scanner.alpha.v5_policy import alphaops_strategy_contract
from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import SnapshotValidationError, StorageError
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.services import (
    price_observation_service,
    screener_automation,
    web_collection_service,
)
from intraday_scanner.services.alpha_outcome_capture_service import (
    capture_sourced_alpha_outcomes,
)
from intraday_scanner.services.alpha_paper_reconciliation_service import (
    reconcile_alpha_paper_trades,
)
from intraday_scanner.services.price_observation_service import collect_price_observations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tree(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    if not root.exists():
        return (), {}
    directories = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        )
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    return directories, files


def _write_minute_bars(path: Path) -> None:
    path.write_text(
        "ticker,timestamp,open,high,low,close,volume\n"
        "NOVA,2026-06-22T09:34:00-04:00,10,11,9,10.5,1000\n",
        encoding="utf-8",
    )


def _seed_historical_signal(store: SQLiteScanStore) -> None:
    store.persist_historical_signals(
        [
            {
                "signal_id": "sig-NOVA",
                "scan_id": "scan-1",
                "generated_at": "2026-06-22T13:20:00+00:00",
                "market_date": "2026-06-22",
                "ticker": "NOVA",
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


def _seed_persisted_scan(db_path: Path, out_dir: Path) -> None:
    assert (
        cli.main(
            [
                "scan",
                "--snapshot",
                str(REPO_ROOT / "sample_data" / "premarket_snapshot_sample.csv"),
                "--out-dir",
                str(out_dir),
                "--db-path",
                str(db_path),
                "--persist",
            ]
        )
        == 0
    )


def _seed_contradictory_alpha_selections(store: SQLiteScanStore) -> None:
    selected_at = "2026-06-22T13:00:00Z"
    strategy_id, strategy_version = alphaops_strategy_contract(selected_at)
    common = {
        "scan_id": "scan-contradictory",
        "rank": 1,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "cohort": "official_telegram",
        "selected_at": selected_at,
    }
    store.persist_signal_selections(
        [
            {
                **common,
                "selection_id": "selection-nova",
                "signal_id": "signal-nova",
                "ticker": "NOVA",
                "decision": "clean_edge",
                "event_key": "alphaops:signal-nova:alpha_morning_watch",
                "body_sha256": "body-nova",
            },
            {
                **common,
                "selection_id": "selection-no-trade",
                "signal_id": "signal-no-trade",
                "ticker": "NO_TRADE",
                "decision": "no_trade",
                "event_key": "alphaops:no-trade:alpha_morning_watch",
                "body_sha256": "body-no-trade",
            },
        ]
    )


def _isolate_screener_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screener_automation, "SCREENER_INBOX", tmp_path / "inbox")
    monkeypatch.setattr(screener_automation, "SCREENER_PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(screener_automation, "SCREENER_FAILED", tmp_path / "failed")
    monkeypatch.setattr(screener_automation, "MANUAL_DATA_DIR", tmp_path / "manual")
    monkeypatch.setattr(screener_automation, "AUTO_SHADOW_OUT", tmp_path / "auto-shadow")
    monkeypatch.setattr(screener_automation, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(screener_automation, "LOG_PATH", tmp_path / "logs" / "attempt.log")


def test_app_monitor_csv_fallback_does_not_create_missing_database(tmp_path: Path) -> None:
    monitor_dir = tmp_path / "monitor"
    monitor_dir.mkdir()
    (monitor_dir / "setup_monitor_checks.csv").write_text(
        "ticker,status\nNOVA,watching\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "missing-db" / "state.sqlite"
    before = _tree(tmp_path)

    rows = dashboard_app._load_monitor_rows(str(db_path), str(monitor_dir))

    assert rows == [{"ticker": "NOVA", "status": "watching"}]
    assert _tree(tmp_path) == before


def test_app_alert_preview_missing_database_is_exact_tree_no_op(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-db" / "state.sqlite"
    before = _tree(tmp_path)

    with pytest.raises(StorageError, match="does not exist"):
        dashboard_app._preview_web_alerts(
            str(db_path),
            ScannerConfig(database_path=db_path),
        )

    assert _tree(tmp_path) == before


def test_app_monitor_existing_database_is_full_tree_read_only(tmp_path: Path) -> None:
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    SQLiteScanStore(db_path).initialize()
    monitor_dir = tmp_path / "monitor"
    monitor_dir.mkdir()
    (monitor_dir / "setup_monitor_checks.csv").write_text(
        "ticker,status\nNOVA,watching\n",
        encoding="utf-8",
    )
    before = _tree(db_root)

    rows = dashboard_app._load_monitor_rows(str(db_path), str(monitor_dir))

    assert rows == [{"ticker": "NOVA", "status": "watching"}]
    assert _tree(db_root) == before


def test_app_alert_preview_existing_database_is_full_tree_read_only(tmp_path: Path) -> None:
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    _seed_persisted_scan(db_path, tmp_path / "seed-scan")
    before = _tree(db_root)

    result = dashboard_app._preview_web_alerts(
        str(db_path),
        ScannerConfig(database_path=db_path),
    )

    assert result["status"] == "ok"
    assert _tree(db_root) == before


def test_explicit_price_preview_does_not_create_database_or_parent(tmp_path: Path) -> None:
    bars = tmp_path / "bars.csv"
    _write_minute_bars(bars)
    db_path = tmp_path / "missing-db" / "state.sqlite"

    result = collect_price_observations(
        db_path=db_path,
        source="csv",
        tickers=["NOVA"],
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=bars,
        persist=False,
        config=ScannerConfig(database_path=db_path),
    )

    assert result["status"] == "ok"
    assert result["persisted"]["inserted"] == 0
    assert not db_path.parent.exists()


@pytest.mark.parametrize("via_cli", (False, True))
def test_explicit_price_preview_never_opens_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via_cli: bool,
) -> None:
    bars = tmp_path / "bars.csv"
    _write_minute_bars(bars)
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    SQLiteScanStore(db_path).initialize()
    before = _tree(db_root)

    def forbidden_store(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("explicit no-persist price observation must not open SQLite")

    monkeypatch.setattr(price_observation_service, "SQLiteScanStore", forbidden_store)
    if via_cli:
        status = cli.main(
            [
                "price-observe",
                "--source",
                "csv",
                "--tickers",
                "NOVA",
                "--db-path",
                str(db_path),
                "--minute-bars",
                str(bars),
                "--market-date",
                "2026-06-22",
                "--at",
                "09:35",
                "--no-persist",
            ]
        )
        assert status == 0
    else:
        result = collect_price_observations(
            db_path=db_path,
            source="csv",
            tickers=["NOVA"],
            market_date="2026-06-22",
            requested_at="09:35",
            minute_bars=bars,
            persist=False,
            config=ScannerConfig(database_path=db_path),
        )
        assert result["status"] == "ok"

    assert _tree(db_root) == before


def test_explicit_price_cli_no_persist_does_not_create_database_or_parent(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    _write_minute_bars(bars)
    db_path = tmp_path / "missing-db" / "state.sqlite"

    status = cli.main(
        [
            "price-observe",
            "--source",
            "csv",
            "--tickers",
            "NOVA",
            "--db-path",
            str(db_path),
            "--minute-bars",
            str(bars),
            "--market-date",
            "2026-06-22",
            "--at",
            "09:35",
            "--no-persist",
        ]
    )

    assert status == 0
    assert not db_path.parent.exists()


@pytest.mark.parametrize("via_cli", (False, True))
def test_implicit_price_preview_missing_database_is_exact_tree_no_op(
    tmp_path: Path,
    via_cli: bool,
) -> None:
    bars = tmp_path / "bars.csv"
    _write_minute_bars(bars)
    db_path = tmp_path / "missing-db" / "state.sqlite"
    before = _tree(tmp_path)

    if via_cli:
        status = cli.main(
            [
                "price-observe",
                "--source",
                "csv",
                "--db-path",
                str(db_path),
                "--minute-bars",
                str(bars),
                "--market-date",
                "2026-06-22",
                "--at",
                "09:35",
                "--no-persist",
            ]
        )
        assert status == 1
    else:
        with pytest.raises(StorageError, match="does not exist"):
            collect_price_observations(
                db_path=db_path,
                source="csv",
                tickers=None,
                market_date="2026-06-22",
                requested_at="09:35",
                minute_bars=bars,
                persist=False,
                config=ScannerConfig(database_path=db_path),
            )

    assert _tree(tmp_path) == before


def test_implicit_price_preview_preserves_existing_database_bytes(tmp_path: Path) -> None:
    bars = tmp_path / "bars.csv"
    _write_minute_bars(bars)
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    _seed_historical_signal(SQLiteScanStore(db_path))
    before = _tree(db_root)

    result = collect_price_observations(
        db_path=db_path,
        source="csv",
        tickers=None,
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=bars,
        persist=False,
        config=ScannerConfig(database_path=db_path),
    )

    assert result["status"] == "ok"
    assert _tree(db_root) == before


def test_implicit_price_cli_rejects_active_wal_without_database_or_output_mutation(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    _write_minute_bars(bars)
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    _seed_historical_signal(SQLiteScanStore(db_path))
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE wal_guard (value TEXT NOT NULL)")
        writer.execute("INSERT INTO wal_guard VALUES ('committed')")
        writer.commit()
        assert Path(f"{db_path}-wal").is_file()
        assert Path(f"{db_path}-shm").is_file()
        before = _tree(db_root)
        out_dir = tmp_path / "price-output"

        status = cli.main(
            [
                "price-observe",
                "--source",
                "csv",
                "--db-path",
                str(db_path),
                "--minute-bars",
                str(bars),
                "--market-date",
                "2026-06-22",
                "--at",
                "09:35",
                "--no-persist",
            ]
        )

        assert status == 1
        assert not out_dir.exists()
        assert _tree(db_root) == before
    finally:
        writer.close()


def test_implicit_price_cli_rejects_dormant_wal_header_without_tree_mutation(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    _write_minute_bars(bars)
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    _seed_historical_signal(SQLiteScanStore(db_path))
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        writer.close()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()
    before = _tree(db_root)

    status = cli.main(
        [
            "price-observe",
            "--source",
            "csv",
            "--db-path",
            str(db_path),
            "--minute-bars",
            str(bars),
            "--market-date",
            "2026-06-22",
            "--at",
            "09:35",
            "--no-persist",
        ]
    )

    assert status == 1
    assert _tree(db_root) == before


def test_free_shadow_default_does_not_create_database_or_parent(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-db" / "state.sqlite"
    out_dir = tmp_path / "scan"

    status = cli.main(
        [
            "free-shadow-scan",
            "--snapshot",
            str(REPO_ROOT / "sample_data" / "premarket_snapshot_sample.csv"),
            "--db-path",
            str(db_path),
            "--out-dir",
            str(out_dir),
        ]
    )

    assert status == 0
    assert (out_dir / "scan_summary.json").is_file()
    assert not db_path.parent.exists()


def test_free_shadow_default_never_opens_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    SQLiteScanStore(db_path).initialize()
    before = _tree(db_root)

    def forbidden_store(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("free-shadow no-persist must not open SQLite")

    monkeypatch.setattr(cli, "SQLiteScanStore", forbidden_store)
    out_dir = tmp_path / "scan"

    status = cli.main(
        [
            "free-shadow-scan",
            "--snapshot",
            str(REPO_ROOT / "sample_data" / "premarket_snapshot_sample.csv"),
            "--db-path",
            str(db_path),
            "--out-dir",
            str(out_dir),
        ]
    )

    assert status == 0
    assert (out_dir / "scan_summary.json").is_file()
    assert _tree(db_root) == before


def test_live_scan_no_persist_does_not_create_database_or_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = read_snapshot_csv(REPO_ROOT / "sample_data" / "premarket_snapshot_sample.csv")

    class FakeAlpacaProvider:
        def __init__(self, _config: ScannerConfig) -> None:
            pass

        def validate_credentials(self) -> None:
            return None

        def get_premarket_snapshot(
            self,
            _symbols: list[str],
            _config: ScannerConfig,
        ) -> list[object]:
            return list(snapshots[:1])

    monkeypatch.setattr(cli, "AlpacaProvider", FakeAlpacaProvider)
    db_path = tmp_path / "missing-db" / "state.sqlite"
    out_dir = tmp_path / "live"

    status = cli.main(
        [
            "live-scan",
            "--symbols",
            "NOVA",
            "--db-path",
            str(db_path),
            "--out-dir",
            str(out_dir),
        ]
    )

    assert status == 0
    assert (out_dir / "scan_summary.json").is_file()
    assert not db_path.parent.exists()


def test_live_scan_no_persist_never_opens_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = read_snapshot_csv(REPO_ROOT / "sample_data" / "premarket_snapshot_sample.csv")

    class FakeAlpacaProvider:
        def __init__(self, _config: ScannerConfig) -> None:
            pass

        def validate_credentials(self) -> None:
            return None

        def get_premarket_snapshot(
            self,
            _symbols: list[str],
            _config: ScannerConfig,
        ) -> list[object]:
            return list(snapshots[:1])

    def forbidden_store(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("live no-persist must not open SQLite")

    monkeypatch.setattr(cli, "AlpacaProvider", FakeAlpacaProvider)
    monkeypatch.setattr(cli, "SQLiteScanStore", forbidden_store)
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    SQLiteScanStore(db_path).initialize()
    before = _tree(db_root)
    out_dir = tmp_path / "live"

    status = cli.main(
        [
            "live-scan",
            "--symbols",
            "NOVA",
            "--db-path",
            str(db_path),
            "--out-dir",
            str(out_dir),
        ]
    )

    assert status == 0
    assert (out_dir / "scan_summary.json").is_file()
    assert _tree(db_root) == before
def test_live_scan_no_persist_missing_credentials_is_exact_tree_no_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    db_path = tmp_path / "missing-db" / "state.sqlite"
    out_dir = tmp_path / "live"
    before = _tree(tmp_path)

    status = cli.main(
        [
            "live-scan",
            "--symbols",
            "NOVA",
            "--db-path",
            str(db_path),
            "--out-dir",
            str(out_dir),
        ]
    )

    assert status == 1
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    ("command", "extra"),
    (
        ("monitor-setups", ()),
        ("monitor-loop", ("--max-iterations", "1")),
        ("monitor-open", ("--max-iterations", "1")),
    ),
)
def test_monitor_no_persist_missing_database_fails_before_artifacts(
    tmp_path: Path,
    command: str,
    extra: tuple[str, ...],
) -> None:
    db_path = tmp_path / "missing-db" / "state.sqlite"
    out_dir = tmp_path / "monitor"
    before = _tree(tmp_path)

    status = cli.main(
        [
            command,
            "--snapshot",
            str(REPO_ROOT / "sample_data" / "premarket_snapshot_sample.csv"),
            "--db-path",
            str(db_path),
            "--out-dir",
            str(out_dir),
            *extra,
        ]
    )

    assert status == 1
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    ("command", "extra"),
    (
        ("monitor-setups", ()),
        ("monitor-loop", ("--max-iterations", "1")),
        ("monitor-open", ("--max-iterations", "1")),
    ),
)
def test_monitor_no_persist_preserves_existing_database_bytes(
    tmp_path: Path,
    command: str,
    extra: tuple[str, ...],
) -> None:
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    assert (
        cli.main(
            [
                "scan",
                "--snapshot",
                str(REPO_ROOT / "sample_data" / "premarket_snapshot_sample.csv"),
                "--out-dir",
                str(tmp_path / "seed-scan"),
                "--db-path",
                str(db_path),
                "--persist",
            ]
        )
        == 0
    )
    before = _tree(db_root)

    status = cli.main(
        [
            command,
            "--snapshot",
            str(REPO_ROOT / "sample_data" / "premarket_snapshot_sample.csv"),
            "--db-path",
            str(db_path),
            "--out-dir",
            str(tmp_path / "monitor"),
            *extra,
        ]
    )

    assert status == 0
    assert _tree(db_root) == before


@pytest.mark.parametrize("operation", ("capture", "reconcile"))
def test_alpha_no_persist_missing_database_fails_before_artifacts(
    tmp_path: Path,
    operation: str,
) -> None:
    db_path = tmp_path / "missing-db" / "state.sqlite"
    out_dir = tmp_path / "out"
    before = _tree(tmp_path)

    with pytest.raises(StorageError, match="does not exist"):
        if operation == "capture":
            capture_sourced_alpha_outcomes(
                db_path=db_path,
                market_date="2026-06-22",
                requested_at="2026-06-22T17:00:00-04:00",
                out_dir=out_dir,
                persist=False,
            )
        else:
            reconcile_alpha_paper_trades(
                db_path=db_path,
                market_date="2026-06-22",
                out_dir=out_dir,
                persist=False,
            )

    assert _tree(tmp_path) == before


@pytest.mark.parametrize("operation", ("capture", "reconcile"))
def test_alpha_cli_no_persist_missing_database_fails_before_artifacts(
    tmp_path: Path,
    operation: str,
) -> None:
    db_path = tmp_path / "missing-db" / "state.sqlite"
    out_dir = tmp_path / "out"
    command = "alpha-capture-outcomes" if operation == "capture" else "alpha-paper-reconcile"
    argv = [
        command,
        "--db-path",
        str(db_path),
        "--market-date",
        "2026-06-22",
        "--out-dir",
        str(out_dir),
    ]
    if operation == "capture":
        argv.extend(["--at", "2026-06-22T17:00:00-04:00"])
    before = _tree(tmp_path)

    status = cli.main(argv)

    assert status == 1
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("evidence", ("absent", "contradictory"))
@pytest.mark.parametrize("via_cli", (False, True))
def test_alpha_capture_invalid_existing_evidence_is_exact_tree_no_op(
    tmp_path: Path,
    evidence: str,
    via_cli: bool,
) -> None:
    db_path = tmp_path / "state.sqlite"
    store = SQLiteScanStore(db_path)
    store.initialize()
    if evidence == "contradictory":
        _seed_contradictory_alpha_selections(store)
    out_dir = tmp_path / "out"
    before = _tree(tmp_path)

    if via_cli:
        status = cli.main(
            [
                "alpha-capture-outcomes",
                "--db-path",
                str(db_path),
                "--market-date",
                "2026-06-22",
                "--at",
                "2026-06-22T17:00:00-04:00",
                "--out-dir",
                str(out_dir),
            ]
        )
        assert status == 1
    else:
        with pytest.raises(
            SnapshotValidationError,
            match="frozen official cohort is absent or ambiguous",
        ):
            capture_sourced_alpha_outcomes(
                db_path=db_path,
                market_date="2026-06-22",
                requested_at="2026-06-22T17:00:00-04:00",
                out_dir=out_dir,
                persist=False,
            )

    assert _tree(tmp_path) == before


@pytest.mark.parametrize("via_cli", (False, True))
def test_alpha_reconcile_empty_existing_database_is_exact_tree_no_op(
    tmp_path: Path,
    via_cli: bool,
) -> None:
    db_path = tmp_path / "state.sqlite"
    SQLiteScanStore(db_path).initialize()
    out_dir = tmp_path / "out"
    before = _tree(tmp_path)

    if via_cli:
        status = cli.main(
            [
                "alpha-paper-reconcile",
                "--db-path",
                str(db_path),
                "--market-date",
                "2026-06-22",
                "--out-dir",
                str(out_dir),
            ]
        )
        assert status == 1
    else:
        with pytest.raises(SnapshotValidationError, match="selection evidence is absent"):
            reconcile_alpha_paper_trades(
                db_path=db_path,
                market_date="2026-06-22",
                out_dir=out_dir,
                persist=False,
            )

    assert _tree(tmp_path) == before


def test_web_sec_explicit_tickers_no_persist_never_opens_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "missing-db" / "state.sqlite"
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        web_collection_service,
        "collect_sec_risk",
        lambda **kwargs: calls.append(kwargs) or {"status": "empty"},
    )

    result = web_collection_service.web_collect_sec_risk(
        config_path=REPO_ROOT / "tests" / "fixtures" / "web_sources_fixture.yaml",
        db_path=db_path,
        out_dir=tmp_path / "out",
        tickers=["NOVA"],
        persist=False,
    )

    assert result["status"] == "empty"
    assert calls[0]["store"] is None
    assert not db_path.parent.exists()


@pytest.mark.parametrize("via_cli", (False, True))
def test_web_sec_explicit_tickers_never_open_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via_cli: bool,
) -> None:
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    SQLiteScanStore(db_path).initialize()
    before = _tree(db_root)
    calls: list[dict[str, object]] = []

    def forbidden_store(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("explicit SEC collection must not open SQLite")

    monkeypatch.setattr(web_collection_service, "SQLiteScanStore", forbidden_store)
    monkeypatch.setattr(
        web_collection_service,
        "collect_sec_risk",
        lambda **kwargs: calls.append(kwargs) or {"status": "empty"},
    )
    config_path = REPO_ROOT / "tests" / "fixtures" / "web_sources_fixture.yaml"
    out_dir = tmp_path / "out"

    if via_cli:
        status = cli.main(
            [
                "web-collect-sec-risk",
                "--config",
                str(config_path),
                "--db-path",
                str(db_path),
                "--out-dir",
                str(out_dir),
                "--tickers",
                "NOVA",
            ]
        )
        assert status == 0
    else:
        result = web_collection_service.web_collect_sec_risk(
            config_path=config_path,
            db_path=db_path,
            out_dir=out_dir,
            tickers=["NOVA"],
            persist=False,
        )
        assert result["status"] == "empty"

    assert calls[0]["store"] is None
    assert _tree(db_root) == before


@pytest.mark.parametrize("via_cli", (False, True))
def test_web_sec_default_discovery_missing_database_is_exact_tree_no_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via_cli: bool,
) -> None:
    db_path = tmp_path / "missing-db" / "state.sqlite"
    out_dir = tmp_path / "out"
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        web_collection_service,
        "collect_sec_risk",
        lambda **kwargs: calls.append(kwargs) or {"status": "empty"},
    )
    before = _tree(tmp_path)

    if via_cli:
        status = cli.main(
            [
                "web-collect-sec-risk",
                "--config",
                str(REPO_ROOT / "tests" / "fixtures" / "web_sources_fixture.yaml"),
                "--db-path",
                str(db_path),
                "--out-dir",
                str(out_dir),
            ]
        )
        assert status == 1
    else:
        with pytest.raises(StorageError, match="does not exist"):
            web_collection_service.web_collect_sec_risk(
                config_path=REPO_ROOT / "tests" / "fixtures" / "web_sources_fixture.yaml",
                db_path=db_path,
                out_dir=out_dir,
                persist=False,
            )

    assert calls == []
    assert _tree(tmp_path) == before


def test_web_sec_default_discovery_preserves_existing_database_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    _seed_persisted_scan(db_path, tmp_path / "seed-scan")
    before = _tree(db_root)
    calls: list[dict[str, object]] = []
    read_only_flags: list[bool] = []
    real_store = web_collection_service.SQLiteScanStore

    class RecordingStore(real_store):
        def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
            read_only_flags.append(read_only)
            super().__init__(path, read_only=read_only)

    monkeypatch.setattr(web_collection_service, "SQLiteScanStore", RecordingStore)
    monkeypatch.setattr(
        web_collection_service,
        "collect_sec_risk",
        lambda **kwargs: calls.append(kwargs) or {"status": "empty"},
    )

    result = web_collection_service.web_collect_sec_risk(
        config_path=REPO_ROOT / "tests" / "fixtures" / "web_sources_fixture.yaml",
        db_path=db_path,
        out_dir=tmp_path / "out",
        tickers=None,
        persist=False,
    )

    assert result["status"] == "empty"
    assert calls
    assert read_only_flags == [True]
    assert calls[0]["store"] is None
    assert _tree(db_root) == before


@pytest.mark.parametrize("via_cli", (False, True))
def test_auto_shadow_no_persist_missing_database_fails_before_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via_cli: bool,
) -> None:
    _isolate_screener_paths(tmp_path, monkeypatch)
    source = tmp_path / "source.csv"
    shutil.copy2(REPO_ROOT / "tests" / "fixtures" / "raw_screener_aliases.csv", source)
    db_path = tmp_path / "missing-db" / "state.sqlite"
    out_dir = tmp_path / "out"
    before = _tree(tmp_path)

    if via_cli:
        status = cli.main(
            [
                "auto-shadow-from-screener",
                "--input",
                str(source),
                "--db-path",
                str(db_path),
                "--out-dir",
                str(out_dir),
            ]
        )
        assert status == 1
    else:
        with pytest.raises(StorageError, match="does not exist"):
            screener_automation.auto_shadow_from_screener(
                input_path=source,
                db_path=db_path,
                out_dir=out_dir,
                persist=False,
                move_file=False,
            )

    assert _tree(tmp_path) == before


def test_auto_shadow_duplicate_guard_is_read_only_and_preserves_database_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_screener_paths(tmp_path, monkeypatch)
    source = tmp_path / "source.csv"
    shutil.copy2(REPO_ROOT / "tests" / "fixtures" / "raw_screener_aliases.csv", source)
    file_hash = screener_automation.file_sha256(source)
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    SQLiteScanStore(db_path).persist_screener_automation_run(
        {
            "run_id": "prior-run",
            "file_hash": file_hash,
            "input_path": "prior.csv",
            "status": "success",
            "started_at": "2026-06-22T13:00:00+00:00",
            "completed_at": "2026-06-22T13:01:00+00:00",
        }
    )
    real_store = screener_automation.SQLiteScanStore
    flags: list[bool] = []

    class RecordingStore(real_store):
        def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
            flags.append(read_only)
            super().__init__(path, read_only=read_only)

    monkeypatch.setattr(screener_automation, "SQLiteScanStore", RecordingStore)
    before = _tree(db_root)

    result = screener_automation.auto_shadow_from_screener(
        input_path=source,
        db_path=db_path,
        out_dir=tmp_path / "out",
        persist=False,
        move_file=False,
    )

    assert result["status"] == "skipped_duplicate"
    assert flags == [True]
    assert _tree(db_root) == before


def test_auto_shadow_unique_no_persist_preserves_database_and_allows_file_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_screener_paths(tmp_path, monkeypatch)
    source = tmp_path / "source.csv"
    shutil.copy2(REPO_ROOT / "tests" / "fixtures" / "raw_screener_aliases.csv", source)
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    SQLiteScanStore(db_path).initialize()
    before = _tree(db_root)
    out_dir = tmp_path / "out"

    result = screener_automation.auto_shadow_from_screener(
        input_path=source,
        db_path=db_path,
        out_dir=out_dir,
        persist=False,
    )

    assert result["status"] == "success"
    assert not source.exists()
    assert list((tmp_path / "processed").glob("*processed*.csv"))
    assert (out_dir / "run_summary.json").is_file()
    assert (out_dir / "scan_summary.json").is_file()
    assert _tree(db_root) == before


def test_normalize_db_path_without_persist_leaves_database_parent_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_screener_paths(tmp_path, monkeypatch)
    db_path = tmp_path / "missing-db" / "state.sqlite"
    out_dir = tmp_path / "normalized"
    before = _tree(tmp_path)

    status = cli.main(
        [
            "normalize-screener-file",
            "--input",
            str(REPO_ROOT / "tests" / "fixtures" / "raw_screener_aliases.csv"),
            "--out",
            str(out_dir),
            "--db-path",
            str(db_path),
        ]
    )

    assert status == 0
    after_directories, after_files = _tree(tmp_path)
    before_directories, before_files = before
    assert set(after_directories) - set(before_directories) == {"normalized"}
    assert set(after_files) - set(before_files) == {
        "normalized/normalization_summary.json",
        "normalized/premarket_snapshot.csv",
    }
    assert not db_path.parent.exists()


def test_normalize_without_persist_never_opens_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_screener_paths(tmp_path, monkeypatch)
    db_root = tmp_path / "db-root"
    db_path = db_root / "state.sqlite"
    SQLiteScanStore(db_path).initialize()
    before = _tree(db_root)

    def forbidden_store(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("normalize without persist must not open SQLite")

    monkeypatch.setattr(cli, "SQLiteScanStore", forbidden_store)
    out_dir = tmp_path / "normalized"

    status = cli.main(
        [
            "normalize-screener-file",
            "--input",
            str(REPO_ROOT / "tests" / "fixtures" / "raw_screener_aliases.csv"),
            "--out",
            str(out_dir),
            "--db-path",
            str(db_path),
        ]
    )

    assert status == 0
    assert (out_dir / "premarket_snapshot.csv").is_file()
    assert _tree(db_root) == before
