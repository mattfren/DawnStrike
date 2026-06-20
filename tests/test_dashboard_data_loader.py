from intraday_scanner.config import ScannerConfig
from intraday_scanner.dashboard.data_loader import load_sqlite
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.services.performance_service import build_performance_report
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_dashboard_loads_sqlite_history(tmp_path):
    config = ScannerConfig(database_path=tmp_path / "scanner.sqlite")
    store = SQLiteScanStore(config.database_path)
    provider = CsvSnapshotProvider("sample_data/premarket_snapshot_sample.csv")
    ScanService(provider, store=store).run(config, persist=True)

    data = load_sqlite(config.database_path)

    assert data["ranked_candidates"]
    assert data["scan_history"]
    assert data["scan_history"][0]["top_ticker"] == "NOVA"


def test_dashboard_loads_operating_history(tmp_path):
    config = ScannerConfig(database_path=tmp_path / "scanner.sqlite")
    store = SQLiteScanStore(config.database_path)
    provider = CsvSnapshotProvider("sample_data/premarket_snapshot_sample.csv")
    result = ScanService(provider, store=store).run(config, persist=True)
    trade = {
        "ticker": "NOVA",
        "rank": "1",
        "close_return_pct": "12.5",
        "lunch_return_pct": "5.0",
        "high_return_pct": "25.0",
        "low_drawdown_pct": "-3.0",
    }
    summary = {"run_id": result.run_id, "created_at": "2026-06-20T16:00:00+00:00"}

    store.record_provider_health("csv", "ok", "2026-06-20T13:00:00+00:00", "ready")
    store.persist_monitor_events(
        [
            {
                "ticker": "NOVA",
                "event_type": "momentum_failure",
                "severity": "high",
                "created_at": "2026-06-20T14:35:00+00:00",
            }
        ],
        run_id=result.run_id,
    )
    store.record_alert(
        alert_key=f"{result.run_id}:NOVA:momentum_failure:high",
        event_type="momentum_failure",
        severity="high",
        ticker="NOVA",
        run_id=result.run_id,
        payload={"ticker": "NOVA", "suggested_action": "CAUTION"},
    )
    store.persist_paper_audit(summary, [trade], run_id=result.run_id)
    store.persist_performance_report(build_performance_report([trade], summary))

    data = load_sqlite(config.database_path)

    assert data["provider_health"][0]["status"] == "ok"
    assert data["data_source_kind"] == "sqlite_live_or_persisted"
    assert data["live_readiness"]
    assert data["recent_alerts"][0]["ticker"] == "NOVA"
    assert data["monitor_events"][0]["event_type"] == "momentum_failure"
    assert any(row["ticker"] == "NOVA" for row in data["recommendation_history"])
    assert data["audit_trades"][0]["close_return_pct"] == "12.5"
    assert data["performance_report"]["trade_count"] == 1
