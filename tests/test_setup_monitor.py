from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SnapshotRow
from intraday_scanner.services.setup_monitor import evaluate_setup_monitor
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _candidate() -> dict[str, object]:
    return {
        "rank": 1,
        "ticker": "NOVA",
        "score": 82.5,
        "premarket_price": 5.20,
        "dollar_volume": 7_800_000,
        "breakout_trigger": 5.45,
        "pullback_zone": "4.95-5.20",
        "invalidation_level": 4.80,
        "first_target": 6.10,
        "stretch_target": 6.85,
        "risk_flags": "",
    }


def _snapshot(
    price: float,
    *,
    high: float = 6.0,
    low: float = 4.7,
    halt: bool = False,
    offering: bool = False,
) -> SnapshotRow:
    return SnapshotRow(
        ticker="NOVA",
        company="NovaPulse Therapeutics",
        premarket_price=price,
        previous_close=2.75,
        premarket_high=high,
        premarket_low=low,
        premarket_volume=1_500_000,
        dollar_volume=price * 1_500_000,
        gap_pct=89.0,
        float_shares=18_000_000,
        market_cap=94_000_000,
        spread_pct=1.2,
        short_float_pct=18.5,
        has_news=True,
        catalyst_headline="FDA fast-track catalyst",
        current_halt=halt,
        recent_offering=offering,
        reverse_split_90d=False,
        source="test",
        as_of_timestamp="2026-06-18T09:35:00-04:00",
    )


def test_setup_monitor_confirms_breakout_path():
    rows = evaluate_setup_monitor(candidates=[_candidate()], snapshots=[_snapshot(5.60)])

    assert rows[0]["status"] == "confirming"
    assert rows[0]["expected_return_to_first_pct"] > 0
    assert rows[0]["path_progress_pct"] > 50


def test_setup_monitor_invalidates_on_price_or_hard_risk():
    price_rows = evaluate_setup_monitor(candidates=[_candidate()], snapshots=[_snapshot(4.75)])
    halt_rows = evaluate_setup_monitor(
        candidates=[_candidate()], snapshots=[_snapshot(5.60, halt=True)]
    )

    assert price_rows[0]["status"] == "invalidated"
    assert halt_rows[0]["status"] == "invalidated"
    assert "current_halt" in halt_rows[0]["risk_flags"]


def test_setup_monitor_marks_extended_after_target():
    rows = evaluate_setup_monitor(candidates=[_candidate()], snapshots=[_snapshot(6.25, high=6.4)])

    assert rows[0]["status"] == "extended"
    assert rows[0]["distance_to_first_target_pct"] > 0


def test_setup_monitor_invalidates_configured_drop_from_watch():
    rows = evaluate_setup_monitor(
        candidates=[_candidate()],
        snapshots=[_snapshot(4.85, high=5.2, low=4.8)],
        config=ScannerConfig(monitor_drop_from_watch_pct=5),
    )

    assert rows[0]["status"] == "invalidated"
    assert "drop_from_watch" in rows[0]["risk_flags"]
    assert "watch-price limit" in rows[0]["reason"]


def test_setup_monitor_flags_volume_collapse():
    candidate = {**_candidate(), "dollar_volume": 20_000_000}
    rows = evaluate_setup_monitor(
        candidates=[candidate],
        snapshots=[_snapshot(5.25, high=5.35, low=4.95)],
        config=ScannerConfig(monitor_volume_collapse_ratio=0.5),
    )

    assert rows[0]["status"] == "fading"
    assert "volume_collapse" in rows[0]["risk_flags"]
    assert "Dollar volume collapsed" in rows[0]["reason"]


def test_setup_monitor_flags_breakout_rejection():
    rows = evaluate_setup_monitor(
        candidates=[_candidate()],
        snapshots=[_snapshot(5.25, high=5.55, low=5.0)],
        config=ScannerConfig(monitor_rejection_range_pct=90),
    )

    assert rows[0]["status"] == "fading"
    assert "breakout_rejection" in rows[0]["risk_flags"]
    assert "rejected the breakout" in rows[0]["reason"]


def test_setup_monitor_persists_latest_batch(tmp_path):
    store = SQLiteScanStore(tmp_path / "scanner.sqlite")
    rows = evaluate_setup_monitor(candidates=[_candidate()], snapshots=[_snapshot(5.60)])

    store.persist_monitor_checks(rows, run_id="run-1")

    latest = store.load_latest_monitor_checks()
    assert latest[0]["ticker"] == "NOVA"
    assert latest[0]["status"] == "confirming"
