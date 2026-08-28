from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner import cli
from intraday_scanner.alpha import run_contracts
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.notifiers import NotificationEvent
from intraday_scanner.services import alpha_cycle_service
from intraday_scanner.services.alpha_cycle_service import _persist_official_selections
from intraday_scanner.services.luna_research_slate_service import (
    build_ranked_research_slate,
    persist_ranked_research_slate,
)
from intraday_scanner.services.premarket_enrichment_service import observation_from_alpaca_bars
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _run_source_failure(
    tmp_path: Path,
    monkeypatch,
    *,
    as_of: datetime,
    wall_clock: str,
) -> tuple[dict[str, object], SQLiteScanStore]:
    monkeypatch.setattr(
        alpha_cycle_service,
        "utc_now_iso",
        lambda: wall_clock,
    )
    def failed_collection(**kwargs):
        assert kwargs["observed_at"] == as_of.astimezone(timezone.utc)
        return {
            "status": "failed",
            "source_summary": {
                "status": "source_failed",
                "candidate_count": 0,
                "reason": "hostile-clock source failure regression",
            },
        }

    monkeypatch.setattr(alpha_cycle_service, "web_auto_collect", failed_collection)
    db_path = tmp_path / "alpha.sqlite"
    result = alpha_cycle_service.alpha_cycle(
        config_path="missing.yaml",
        db_path=db_path,
        out_dir=tmp_path / "alpha",
        notify="console",
        dry_run=True,
        as_of=as_of,
    )
    return result, SQLiteScanStore(db_path)


def test_backdated_source_failure_uses_explicit_cycle_timestamp_everywhere(
    tmp_path, monkeypatch
) -> None:
    cycle_timestamp = "2026-08-26T13:00:00+00:00"
    cycle_date = "2026-08-26"
    scan_id = f"alpha_cycle:source_failure:{cycle_date}"

    result, store = _run_source_failure(
        tmp_path,
        monkeypatch,
        as_of=datetime.fromisoformat(cycle_timestamp),
        wall_clock="2026-08-27T23:59:59+00:00",
    )

    assert result["scan_id"] == scan_id
    assert result["core_universe"]["requested_market_date"] == cycle_date
    slate = result["ranked_research_slate"]
    assert slate["scan_id"] == scan_id
    assert slate["market_date"] == cycle_date
    assert slate["generated_at"] == cycle_timestamp
    contract = result["run_contract"]
    assert contract["producer_run_id"] == scan_id
    assert contract["market_date"] == cycle_date
    assert contract["slate_source_scan_id"] == scan_id
    assert contract["slate_market_date"] == cycle_date

    historical = store.load_historical_signals(scan_id=scan_id, limit=10)
    assert len(historical) == 1
    assert historical[0]["generated_at"] == cycle_timestamp
    assert historical[0]["market_date"] == cycle_date
    assert historical[0]["signal_id"] == f"no_trade:{scan_id}:{cycle_date}"

    selections = store.load_signal_selections(scan_id=scan_id, limit=10)
    assert len(selections) == 1
    assert selections[0]["selected_at"] == cycle_timestamp
    deliveries = result["notification_deliveries"]
    assert len(deliveries) == 1
    assert deliveries[0]["scan_id"] == scan_id
    assert deliveries[0]["selected_at"] == cycle_timestamp
    assert deliveries[0]["attempted_at"] == "2026-08-27T23:59:59+00:00"
    notification = next(
        item
        for item in store.load_recent_notifications(limit=10)
        if item["event_key"].endswith(":alpha_no_trade:console")
    )
    assert "Research slate: 0 of 5 shown" in notification["body"]
    assert notification["body"].count("Slate shortfall reason:") == 1
    assert slate["slate_shortfall_reason"] in notification["body"]


def test_source_failure_uses_utc_date_at_rollover_not_hostile_wall_clock(
    tmp_path, monkeypatch
) -> None:
    # The explicit cycle is late on Aug 26 in a -05:00 zone, so its canonical
    # UTC identity belongs to Aug 27. The patched wall clock points elsewhere.
    as_of = datetime(
        2026,
        8,
        26,
        23,
        59,
        59,
        123456,
        tzinfo=timezone(timedelta(hours=-5)),
    )
    cycle_timestamp = "2026-08-27T04:59:59.123456+00:00"
    cycle_date = "2026-08-27"
    scan_id = f"alpha_cycle:source_failure:{cycle_date}"

    result, store = _run_source_failure(
        tmp_path,
        monkeypatch,
        as_of=as_of,
        wall_clock="2026-08-26T23:59:59+00:00",
    )

    assert result["scan_id"] == scan_id
    assert result["ranked_research_slate"]["market_date"] == cycle_date
    assert result["ranked_research_slate"]["generated_at"] == cycle_timestamp
    assert result["run_contract"]["market_date"] == cycle_date
    assert result["run_contract"]["producer_run_id"] == scan_id
    historical = store.load_historical_signals(scan_id=scan_id, limit=10)
    assert historical[0]["generated_at"] == cycle_timestamp
    assert historical[0]["market_date"] == cycle_date


def test_nonempty_frozen_source_retry_reuses_cohort_and_preserves_canonical_artifacts(
    tmp_path, monkeypatch
) -> None:
    cycle_at = datetime(2026, 8, 26, 13, 30, tzinfo=timezone.utc)
    out_dir = tmp_path / "alpha"
    out_dir.mkdir()
    slate = build_ranked_research_slate(
        [{"ticker": "FROZEN", "signal_id": "signal-frozen", "universe_lane": "mover"}],
        generated_at=cycle_at.isoformat(),
        market_date=cycle_at.date().isoformat(),
        scan_id="scan-success",
        lane_statuses={"mover": {"data_eligible": True, "promotion_limited": False}},
    )
    persist_ranked_research_slate(slate, out_dir / "ranked_research_slate.json")
    canonical_cycle = b'{"authoritative":"cycle"}\n'
    canonical_contract = b'{"authoritative":"contract"}\n'
    (out_dir / "alpha_cycle.json").write_bytes(canonical_cycle)
    (out_dir / "alpha_run_contract.json").write_bytes(canonical_contract)
    store = SQLiteScanStore(tmp_path / "alpha.sqlite")
    frozen_row = dict(slate["rows"][0])
    _persist_official_selections(
        store,
        scan_id="scan-success",
        selected_signals=[frozen_row],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=cycle_at.isoformat(),
        event=NotificationEvent(
            event_key="alphaops:scan-success:alpha_morning_watch",
            title="Dawnstrike Alpha Watch",
            body="immutable frozen publication",
            channel_hint="alpha_morning_watch",
        ),
        slate=slate,
    )

    class PersistedResolver:
        def verify(self, row):
            return row.get("publication_tier") == "PAPER_PLAN_QUALIFIED"

    dispatch_calls = []
    monkeypatch.setattr(
        alpha_cycle_service,
        "_persisted_strategy_receipt_verifier",
        lambda *args, **kwargs: PersistedResolver(),
    )
    def publish(rows, **_kwargs):
        published = [dict(row) for row in rows]
        for row in published:
            row["publication_tier"] = "PAPER_PLAN_QUALIFIED"
            row["plan_qualification_status"] = "QUALIFIED"
            row["alert_gate_status"] = "PASS"
            row["manual_confirmation_required"] = False
            row["source_scan_id"] = "scan-success"
        return published

    monkeypatch.setattr(alpha_cycle_service, "apply_publication_semantics", publish)
    monkeypatch.setattr(run_contracts, "apply_publication_semantics", publish)
    monkeypatch.setattr(
        alpha_cycle_service,
        "_dispatch",
        lambda *args, **kwargs: dispatch_calls.append(args[0])
        or {"sent": 0, "skipped": 0, "errors": []},
    )
    result, _ = _run_source_failure(
        tmp_path,
        monkeypatch,
        as_of=cycle_at,
        wall_clock="2026-08-27T23:59:59+00:00",
    )

    assert result["status"] == "source_failed_retry"
    assert (
        result["source_summary"]["ranked_research_publication_rows"][0]["publication_tier"]
        == "PAPER_PLAN_QUALIFIED"
    )
    assert result["run_contract"]["ranked_research_count"] == 1
    assert result["selection_stats"]["official_cohort_reused"] is True
    assert (
        result["notification_deliveries"][0]["event_key"]
        == "alphaops:scan-success:alpha_morning_watch"
    )
    assert dispatch_calls[0][0].body == "immutable frozen publication"
    assert [row["ticker"] for row in dispatch_calls[0][0].payload["signals"]] == ["FROZEN"]
    assert len(dispatch_calls) == 1
    assert (out_dir / "alpha_run_contract_retry_attempt.json").exists()
    assert (out_dir / "alpha_cycle_retry_attempt.json").exists()
    assert (out_dir / "alpha_cycle.json").read_bytes() == canonical_cycle
    assert (out_dir / "alpha_run_contract.json").read_bytes() == canonical_contract


def test_frozen_source_retry_missing_evidence_fails_before_dispatch_and_preserves_artifacts(
    tmp_path, monkeypatch
) -> None:
    cycle_at = datetime(2026, 8, 26, 13, 30, tzinfo=timezone.utc)
    out_dir = tmp_path / "alpha"
    out_dir.mkdir()
    slate = build_ranked_research_slate(
        [{"ticker": "FROZEN", "signal_id": "signal-frozen", "universe_lane": "mover"}],
        generated_at=cycle_at.isoformat(),
        market_date=cycle_at.date().isoformat(),
        scan_id="scan-success",
        lane_statuses={"mover": {"data_eligible": True, "promotion_limited": False}},
    )
    persist_ranked_research_slate(slate, out_dir / "ranked_research_slate.json")
    canonical_cycle = b'{"authoritative":"cycle"}\n'
    canonical_contract = b'{"authoritative":"contract"}\n'
    (out_dir / "alpha_cycle.json").write_bytes(canonical_cycle)
    (out_dir / "alpha_run_contract.json").write_bytes(canonical_contract)
    dispatch_calls = []
    monkeypatch.setattr(
        alpha_cycle_service,
        "apply_publication_semantics",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SnapshotValidationError("persisted receipt missing")
        ),
    )
    monkeypatch.setattr(
        alpha_cycle_service,
        "_dispatch",
        lambda *args, **kwargs: dispatch_calls.append(args[0]),
    )

    with pytest.raises(SnapshotValidationError, match="FROZEN_SLATE_PUBLICATION_EVIDENCE_MISSING"):
        _run_source_failure(
            tmp_path,
            monkeypatch,
            as_of=cycle_at,
            wall_clock="2026-08-27T23:59:59+00:00",
        )
    assert dispatch_calls == []
    assert (out_dir / "alpha_cycle.json").read_bytes() == canonical_cycle
    assert (out_dir / "alpha_run_contract.json").read_bytes() == canonical_contract


def test_cli_forwards_explicit_cycle_timestamp_to_alpha_cycle(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_alpha_cycle(**kwargs):
        captured.update(kwargs)
        return {"status": "no_trade"}

    monkeypatch.setattr(cli, "alpha_cycle", fake_alpha_cycle)
    args = SimpleNamespace(
        config="fixture.yaml",
        db_path="alpha.sqlite",
        out_dir="alpha",
        notify="console",
        dry_run=True,
        core_universe_manifest=None,
        market_date="2026-08-27",
        as_of="2026-08-27T13:00:00Z",
    )

    assert cli._run_alpha_cycle(args) == 0
    assert captured["market_date"] == "2026-08-27"
    assert captured["as_of"] == datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)
    capsys.readouterr()


def test_scheduled_0800_ct_cycle_accepts_fresh_1258z_alpaca_bar() -> None:
    cycle_timestamp = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)
    observation = observation_from_alpaca_bars(
        "NOVA",
        [
            {
                "timestamp": "2026-08-27T12:58:00Z",
                "high": 8.2,
                "low": 7.8,
                "close": 8.0,
                "volume": 1_000,
            }
        ],
        previous_close=5.0,
        requested_at=cycle_timestamp,
        max_age_seconds=600,
        feed="iex",
    )

    assert observation.status == "verified"
    assert observation.is_usable is True
    assert observation.bar_completed_at == "2026-08-27T12:59:00+00:00"
    payload = observation.premarket_raw_payload_json
    assert '"requested_at":"2026-08-27T13:00:00+00:00"' in payload


def test_morning_wrapper_passes_one_live_utc_cycle_timestamp() -> None:
    script = Path("scripts/run_alphaops_morning.ps1").read_text(encoding="utf-8")

    assert '$cycleObservedAt = (Get-Date).ToUniversalTime().ToString("o")' in script
    assert '"--as-of", $cycleObservedAt' in script
